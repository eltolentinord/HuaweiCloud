# coding: utf-8
"""Catálogo persistido de precios OFICIALES y flavors ECS por región.

Solo se escriben datos devueltos por las APIs oficiales de Huawei Cloud (BSS y
ECS ``ListFlavors``), siempre con su fuente y su fecha de consulta. Es información
pública de lista: no contiene descuentos ni datos de ningún cliente.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from db.models import (
    BssCodeCatalogEntry,
    FlavorCatalogEntry,
    PriceCatalogEntry,
    RdsFlavorCatalogEntry,
    VolumeTypeCatalogEntry,
)


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
    # Detalle de ``os_extra_specs`` (ver costs/huawei_pricing.fetch_flavors).
    status: Optional[str] = None
    az_status: Optional[Dict[str, str]] = None
    architecture: Optional[str] = None
    cpu_name: Optional[str] = None
    gpu_name: Optional[str] = None
    max_bandwidth_gbps: Optional[Decimal] = None
    max_pps: Optional[int] = None


@dataclass(frozen=True)
class VolumeType:
    """Tipo de disco EVS de la región: zonas que lo ofrecen y zonas donde está agotado."""

    name: str
    availability_zones: Tuple[str, ...] = field(default_factory=tuple)
    sold_out_zones: Tuple[str, ...] = field(default_factory=tuple)


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
                                                status=f.status, az_status=f.az_status or None,
                                                architecture=f.architecture, cpu_name=f.cpu_name,
                                                gpu_name=f.gpu_name, max_bandwidth_gbps=f.max_bandwidth_gbps,
                                                max_pps=f.max_pps, fetched_at=fetched_at))
        self.session.flush()
        return len(unique)

    # ------------------------------------------------------------ tipos de disco
    def volume_types(self, region: str) -> List[VolumeTypeCatalogEntry]:
        return list(self.session.scalars(select(VolumeTypeCatalogEntry)
                                         .where(VolumeTypeCatalogEntry.region == region)
                                         .order_by(VolumeTypeCatalogEntry.name)))

    def replace_volume_types(self, region: str, types: Iterable[VolumeType], *, fetched_at: datetime) -> int:
        """Sustituye los tipos de disco de la región por los recién consultados."""
        self.session.execute(delete(VolumeTypeCatalogEntry).where(VolumeTypeCatalogEntry.region == region))
        unique = {t.name: t for t in types}
        for t in unique.values():
            self.session.add(VolumeTypeCatalogEntry(region=region, name=t.name,
                                                    availability_zones=list(t.availability_zones),
                                                    sold_out_zones=list(t.sold_out_zones), fetched_at=fetched_at))
        self.session.flush()
        return len(unique)


    # ------------------------------------------------------------ RDS
    def rds_flavors(self, region: str, engine: str, version: Optional[str] = None) -> List[RdsFlavorCatalogEntry]:
        query = select(RdsFlavorCatalogEntry).where(RdsFlavorCatalogEntry.region == region,
                                                    RdsFlavorCatalogEntry.engine == engine)
        if version:
            query = query.where(RdsFlavorCatalogEntry.engine_version == version)
        return list(self.session.scalars(query.order_by(RdsFlavorCatalogEntry.vcpus, RdsFlavorCatalogEntry.ram_gb,
                                                        RdsFlavorCatalogEntry.spec_code)))

    def rds_versions(self, region: str, engine: str) -> List[str]:
        rows = self.session.scalars(select(RdsFlavorCatalogEntry.engine_version).where(
            RdsFlavorCatalogEntry.region == region, RdsFlavorCatalogEntry.engine == engine).distinct())
        return sorted(set(rows), reverse=True)

    def replace_rds_flavors(self, region: str, engine: str, version: str, flavors: Iterable[Dict],
                            *, fetched_at: datetime) -> int:
        self.session.execute(delete(RdsFlavorCatalogEntry).where(
            RdsFlavorCatalogEntry.region == region, RdsFlavorCatalogEntry.engine == engine,
            RdsFlavorCatalogEntry.engine_version == version))
        unique = {f["spec_code"]: f for f in flavors}
        for f in unique.values():
            self.session.add(RdsFlavorCatalogEntry(region=region, engine=engine, engine_version=version,
                                                   spec_code=f["spec_code"], vcpus=f["vcpus"], ram_gb=f["ram_gb"],
                                                   instance_mode=f["instance_mode"], az_status=f.get("az_status"),
                                                   fetched_at=fetched_at))
        self.session.flush()
        return len(unique)

    # ------------------------------------------------------------ códigos BSS
    def bss_codes(self, kind: Optional[str] = None, parent: Optional[str] = None) -> List[BssCodeCatalogEntry]:
        query = select(BssCodeCatalogEntry)
        if kind:
            query = query.where(BssCodeCatalogEntry.kind == kind)
        if parent:
            query = query.where(BssCodeCatalogEntry.parent_code == parent)
        return list(self.session.scalars(query.order_by(BssCodeCatalogEntry.code)))

    def bss_code_set(self) -> Dict[str, set]:
        out: Dict[str, set] = {"service": set(), "resource": set(), "usage": set()}
        for row in self.session.scalars(select(BssCodeCatalogEntry)):
            out.setdefault(row.kind, set()).add(row.code)
        return out

    def replace_bss_codes(self, codes: Iterable[Dict], *, fetched_at: datetime, kinds: Tuple[str, ...],
                          parent: Optional[str] = None) -> int:
        query = delete(BssCodeCatalogEntry).where(BssCodeCatalogEntry.kind.in_(kinds))
        if parent:
            query = query.where(BssCodeCatalogEntry.parent_code == parent)
        self.session.execute(query)
        unique = {(c["kind"], c["code"]): c for c in codes}
        for c in unique.values():
            self.session.add(BssCodeCatalogEntry(kind=c["kind"], code=str(c["code"])[:128],
                                                 name=(str(c["name"])[:200] if c.get("name") else None),
                                                 parent_code=(str(c["parent_code"])[:128] if c.get("parent_code") else None),
                                                 fetched_at=fetched_at))
        self.session.flush()
        return len(unique)
