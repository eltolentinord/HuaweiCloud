# coding: utf-8
"""Catálogo persistido de precios OFICIALES y flavors ECS por región.

Solo se escriben datos devueltos por las APIs oficiales de Huawei Cloud (BSS y
ECS ``ListFlavors``), siempre con su fuente y su fecha de consulta. Es información
pública de lista: no contiene descuentos ni datos de ningún cliente.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Dict, Iterable, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from db.models import FlavorCatalogEntry, PriceCatalogEntry


@dataclass(frozen=True)
class Quote:
    """Precio oficial de una configuración exacta (lo que devuelve BSS)."""

    region: str
    product: str
    spec: str
    size: int
    billing_mode: str       # on_demand | monthly
    amount: Decimal         # importe para ``period``
    period: str             # hour | month
    currency: str
    fetched_at: datetime
    source: str = "huawei_bss"
    source_detail: str = ""


@dataclass(frozen=True)
class Flavor:
    flavor_id: str
    vcpus: int
    ram_mb: int
    performance_type: Optional[str] = None
    generation: Optional[str] = None


class PriceCatalog:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------ precios
    def lookup(self, *, region: str, product: str, spec: str, billing_mode: str,
               size: int = 0) -> Optional[PriceCatalogEntry]:
        """Precio de la configuración EXACTA (mismo tamaño). Nunca extrapola."""
        return self.session.scalars(select(PriceCatalogEntry).where(
            PriceCatalogEntry.region == region, PriceCatalogEntry.product == product,
            PriceCatalogEntry.spec == spec, PriceCatalogEntry.billing_mode == billing_mode,
            PriceCatalogEntry.size == size).order_by(PriceCatalogEntry.fetched_at.desc()).limit(1)).first()

    def store(self, quotes: Iterable[Quote]) -> int:
        count = 0
        for quote in quotes:
            row = self.session.scalars(select(PriceCatalogEntry).where(
                PriceCatalogEntry.source == quote.source, PriceCatalogEntry.region == quote.region,
                PriceCatalogEntry.product == quote.product, PriceCatalogEntry.spec == quote.spec,
                PriceCatalogEntry.billing_mode == quote.billing_mode, PriceCatalogEntry.size == quote.size)).first()
            if row is None:
                row = PriceCatalogEntry(source=quote.source, region=quote.region, product=quote.product,
                                        spec=quote.spec, billing_mode=quote.billing_mode, size=quote.size)
                self.session.add(row)
            row.amount, row.period, row.currency = quote.amount, quote.period, quote.currency
            row.fetched_at, row.source_detail = quote.fetched_at, quote.source_detail[:200]
            count += 1
        self.session.flush()
        return count

    def counts_by_region(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for region in self.session.scalars(select(PriceCatalogEntry.region)):
            counts[region] = counts.get(region, 0) + 1
        return counts

    # ------------------------------------------------------------ flavors
    def flavors(self, region: str) -> List[FlavorCatalogEntry]:
        return list(self.session.scalars(select(FlavorCatalogEntry).where(FlavorCatalogEntry.region == region)
                                         .order_by(FlavorCatalogEntry.vcpus, FlavorCatalogEntry.ram_mb,
                                                   FlavorCatalogEntry.flavor_id)))

    def flavor(self, region: str, flavor_id: str) -> Optional[FlavorCatalogEntry]:
        return self.session.scalars(select(FlavorCatalogEntry).where(
            FlavorCatalogEntry.region == region, FlavorCatalogEntry.flavor_id == flavor_id)).first()

    def replace_flavors(self, region: str, flavors: Iterable[Flavor], *, fetched_at: datetime) -> int:
        """Sustituye la lista de la región por la recién consultada (la API es la fuente de verdad)."""
        self.session.execute(delete(FlavorCatalogEntry).where(FlavorCatalogEntry.region == region))
        unique = {f.flavor_id: f for f in flavors}
        for f in unique.values():
            self.session.add(FlavorCatalogEntry(region=region, flavor_id=f.flavor_id, vcpus=f.vcpus, ram_mb=f.ram_mb,
                                                performance_type=f.performance_type, generation=f.generation,
                                                fetched_at=fetched_at))
        self.session.flush()
        return len(unique)
