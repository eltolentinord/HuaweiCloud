# coding: utf-8
"""PriceResolver: precio mensual de cada componente SIN inventar valores.

Orden de fuentes:
1. Catálogo de precios oficiales consultados a Huawei Cloud (BSS), por modo de cobro:
   - ``monthly``   precio de suscripción mensual (1 mes).
   - ``on_demand`` precio por hora × ``hours_per_month`` (visible en ``basis``).
2. Tabla de precios oficial del usuario (``INVENTORY_PRICE_TABLE``), con las mismas
   reglas que la estimación del inventario (``costs.pricing``).
3. Si no hay ninguna: "precio no disponible" con el motivo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from typing import Any, Dict, Optional

from costs.catalog import PriceCatalog
from costs.configuration import Component
from costs.pricing import NoPricingProvider, PricingProvider

BILLING_LABELS = {"on_demand": "pago por uso (por hora)", "monthly": "suscripción mensual"}
DEFAULT_HOURS_PER_MONTH = 730

# Componente -> recurso "virtual" equivalente para las reglas de la tabla de precios.
_TABLE_SHAPE = {
    "ecs": ("ecs", "ecs.server"),
    "evs": ("evs", "evs.volume"),
    "ip": ("eip", "eip.publicip"),
    "bandwidth": ("eip", "eip.bandwidth"),
}


@dataclass
class PricedComponent:
    component: Component
    monthly_amount: Optional[Decimal] = None
    currency: Optional[str] = None
    unit_amount: Optional[Decimal] = None
    unit_period: Optional[str] = None
    source: Optional[str] = None
    updated_at: Optional[datetime] = None
    basis: str = ""
    reason: Optional[str] = None

    @property
    def available(self) -> bool:
        return self.monthly_amount is not None

    def as_dict(self) -> Dict[str, Any]:
        c = self.component
        return {"label": c.label, "product": c.product, "spec": c.spec, "size": c.size, "quantity": c.quantity,
                "available": self.available,
                "monthly_amount": (str(self.monthly_amount.quantize(Decimal("0.01"), ROUND_HALF_UP))
                                   if self.monthly_amount is not None else None),
                "currency": self.currency,
                "unit_amount": _plain(self.unit_amount),
                "unit_period": self.unit_period, "source": self.source,
                "updated_at": self.updated_at.isoformat() if self.updated_at else None,
                "basis": self.basis, "reason": self.reason}


def _plain(value: Optional[Decimal]) -> Optional[str]:
    """Importe unitario sin ceros sobrantes (0.05000000 -> 0.05) y sin notación científica."""
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return text if "." not in text or len(text.split(".")[1]) >= 2 else f"{Decimal(text):.2f}"


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _table_attributes(component: Component) -> Dict[str, Any]:
    spec = component.spec or ""
    if component.product == "ecs":
        flavor, _, os_type = spec.rpartition(".")
        return {"flavor_name": flavor, "flavor_id": flavor, "os_type": os_type}
    if component.product == "evs":
        return {"volume_type": spec, "size_gb": component.size}
    if component.product == "ip":
        return {"type": spec}
    return {"bandwidth_type": spec, "bandwidth_size": component.size}


class PriceResolver:
    def __init__(self, catalog: PriceCatalog, *, billing_mode: str = "monthly",
                 hours_per_month: int = DEFAULT_HOURS_PER_MONTH,
                 table: Optional[PricingProvider] = None) -> None:
        if billing_mode not in BILLING_LABELS:
            raise ValueError("billing_mode debe ser on_demand o monthly")
        if not 1 <= hours_per_month <= 744:
            raise ValueError("hours_per_month debe estar entre 1 y 744")
        self.catalog = catalog
        self.billing_mode = billing_mode
        self.hours_per_month = hours_per_month
        self.table = table or NoPricingProvider()

    def price(self, component: Component, region: str) -> PricedComponent:
        result = PricedComponent(component)
        if component.problem or not component.spec:
            result.reason = f"Precio no disponible: {component.problem or 'configuración incompleta'}."
            return result
        entry = self.catalog.lookup(region=region, product=component.product, spec=component.spec,
                                    billing_mode=self.billing_mode, size=component.size)
        if entry is not None:
            amount = Decimal(str(entry.amount))
            per_month = amount * self.hours_per_month if entry.period == "hour" else amount
            unit = "h" if entry.period == "hour" else "mes"
            result.monthly_amount = per_month * component.quantity
            result.currency, result.unit_amount, result.unit_period = entry.currency, amount, entry.period
            result.source = f"Huawei Cloud BSS ({entry.source_detail}), precio oficial de lista"
            result.updated_at = _utc(entry.fetched_at)
            result.basis = f"{_plain(amount)} {entry.currency}/{unit}" + (
                f" × {self.hours_per_month} h" if entry.period == "hour" else "") + (
                f" × {component.quantity}" if component.quantity != 1 else "")
            return result
        service, resource_type = _TABLE_SHAPE[component.product]
        virtual = SimpleNamespace(service=service, resource_type=resource_type, region=region,
                                  attributes=_table_attributes(component))
        quote = self.table.price(virtual)  # type: ignore[arg-type]  # mismo contrato que InventoryResource
        if quote is not None:
            amount, currency, basis = quote
            result.monthly_amount = amount * component.quantity
            result.currency, result.unit_amount, result.unit_period = currency, amount, "month"
            result.source, result.basis = self.table.name, basis
            return result
        result.reason = (f"Precio no disponible: no hay precio oficial de {component.spec}"
                         f"{f' ({component.size})' if component.size else ''} en {region} para "
                         f"{BILLING_LABELS[self.billing_mode]}. Consulta los precios oficiales o carga una tabla oficial.")
        return result
