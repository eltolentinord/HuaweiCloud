# coding: utf-8
"""RegionPriceComparison: recurso(s) REAL(es) del inventario -> configuración destino -> costos.

Solo lectura: el inventario y los recursos de Huawei Cloud nunca se modifican. El
destino es una simulación (otra región, otro flavor, otros discos…).
Aislamiento: los recursos se buscan SIEMPRE dentro de la cuenta (y esta dentro del
cliente) recibida; un ID ajeno se trata como inexistente.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from costs.catalog import PriceCatalog
from costs.comparison import CostComparison, SideCost, difference, money
from costs.configuration import (
    Component,
    ResourceConfiguration,
    complete_origin,
    destination_from_payload,
    origin_from_inventory,
)
from costs.resolver import BILLING_LABELS, PriceResolver
from db.models import CloudAccount, InventoryResource, Project
from repositories import projects as projects_repo
from repositories import resources as resources_repo
from tenancy.errors import NotFoundError

MAX_ITEMS = 50


@dataclass
class ComparisonItem:
    resource: InventoryResource
    origin: ResourceConfiguration
    destination: ResourceConfiguration
    comparison: CostComparison
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        r = self.resource
        return {"resource": {"id": str(r.id), "name": r.name, "provider_id": r.provider_id, "service": r.service,
                             "resource_type": r.resource_type, "region": r.region, "status": r.status},
                "origin_config": self.origin.as_dict(), "destination_config": self.destination.as_dict(),
                "warnings": self.warnings, **self.comparison.as_dict()}


def project_for_region(session: Session, account_id: uuid.UUID, region: str) -> Optional[Project]:
    """Project ID principal de la cuenta en la región (necesario para cotizar con BSS)."""
    candidates = [p for p in projects_repo.list_for_account(session, account_id) if p.region_id == region]
    candidates.sort(key=lambda p: (p.name != region, p.parent_huawei_project_id is not None))
    return candidates[0] if candidates else None


class RegionPriceComparison:
    def __init__(self, session: Session, account: CloudAccount, resolver: PriceResolver,
                 known_regions: Sequence[str]) -> None:
        self.session = session
        self.account = account
        self.resolver = resolver
        self.catalog: PriceCatalog = resolver.catalog
        self.known_regions = list(known_regions)

    def resource(self, resource_id: uuid.UUID) -> InventoryResource:
        row = resources_repo.get_for_account(self.session, self.account.id, resource_id)
        if row is None:
            raise NotFoundError("Recurso no encontrado.")
        return row

    def origin(self, resource_id: uuid.UUID) -> Tuple[InventoryResource, ResourceConfiguration]:
        row = self.resource(resource_id)
        return row, origin_from_inventory(self.session, row)

    def _check_flavor(self, config: ResourceConfiguration) -> List[str]:
        if config.kind != "ecs" or not config.flavor:
            return []
        known = self.catalog.flavors(config.region)
        if not known:
            return [f"No hay lista de flavors consultada para {config.region}: no se puede confirmar que "
                    f"{config.flavor} exista allí."]
        match = next((f for f in known if f.flavor_id == config.flavor), None)
        if match is None:
            return [f"El flavor {config.flavor} no aparece en la lista oficial de {config.region} "
                    f"(consultada {match_date(known)})."]
        warnings = []
        ram_gb = round(match.ram_mb / 1024, 2)
        if (config.vcpus not in (None, match.vcpus)) or (config.ram_gb not in (None, ram_gb)):
            warnings.append(f"El precio depende del flavor: {config.flavor} tiene {match.vcpus} vCPU y {ram_gb:g} GB.")
        config.vcpus, config.ram_gb = match.vcpus, ram_gb
        return warnings

    def _side(self, config: ResourceConfiguration) -> SideCost:
        return SideCost.from_lines([self.resolver.price(c, config.region) for c in config.components()])

    def compare(self, requests: Sequence[Tuple[uuid.UUID, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]], *,
                region: str) -> List[ComparisonItem]:
        """``requests``: ``(resource_id, cambios_destino, datos_desconocidos_del_origen)``."""
        if not requests:
            raise ValueError("Indica al menos un recurso.")
        if len(requests) > MAX_ITEMS:
            raise ValueError(f"Máximo {MAX_ITEMS} recursos por comparación.")
        items = []
        for resource_id, payload, assumptions in requests:
            row, origin = self.origin(resource_id)
            origin = complete_origin(origin, assumptions)
            destination = destination_from_payload(origin, payload, region=region, known_regions=self.known_regions)
            warnings = list(origin.notes) + self._check_flavor(destination)
            items.append(ComparisonItem(resource=row, origin=origin, destination=destination,
                                        comparison=CostComparison(self._side(origin), self._side(destination)),
                                        warnings=warnings))
        return items

    def summary(self, items: Sequence[ComparisonItem]) -> Dict[str, Any]:
        """Totales por moneda SOLO de los recursos comparables (los demás se cuentan aparte)."""
        totals: Dict[str, Dict[str, Decimal]] = {}
        for item in items:
            if item.comparison.comparable:
                currency = item.comparison.origin.currency or ""
                group = totals.setdefault(currency, {"origin": Decimal("0"), "destination": Decimal("0")})
                group["origin"] += item.comparison.origin.total or Decimal("0")
                group["destination"] += item.comparison.destination.total or Decimal("0")
        by_currency = []
        for currency, group in sorted(totals.items()):
            absolute, percent = difference(group["origin"], group["destination"])
            by_currency.append({"currency": currency, "origin": money(group["origin"]),
                                "destination": money(group["destination"]), "difference": money(absolute),
                                "difference_percent": str(percent) if percent is not None else None})
        compared = sum(1 for i in items if i.comparison.comparable)
        return {"items": len(items), "compared": compared, "not_compared": len(items) - compared,
                "by_currency": by_currency, "billing_mode": self.resolver.billing_mode,
                "billing_label": BILLING_LABELS[self.resolver.billing_mode],
                "period": "mensual" + (f" ({self.resolver.hours_per_month} h de uso)"
                                       if self.resolver.billing_mode == "on_demand" else " (1 mes de suscripción)"),
                "hours_per_month": self.resolver.hours_per_month}


def match_date(flavors: Sequence[Any]) -> str:
    dates = [f.fetched_at for f in flavors if getattr(f, "fetched_at", None)]
    return max(dates).strftime("%Y-%m-%d %H:%M UTC") if dates else "fecha desconocida"


def quotable_components(items: Sequence[ComparisonItem]) -> Dict[str, List[Component]]:
    """Componentes cotizables (sin problema) agrupados por región, sin duplicados."""
    by_region: Dict[str, Dict[Tuple[str, str, int], Component]] = {}
    for item in items:
        for config in (item.origin, item.destination):
            for c in config.components():
                if c.spec and not c.problem:
                    by_region.setdefault(config.region, {})[(c.product, c.spec, c.size)] = Component(
                        c.product, c.spec, size=c.size, label=c.label)
    return {region: list(parts.values()) for region, parts in by_region.items()}
