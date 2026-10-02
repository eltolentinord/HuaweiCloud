# coding: utf-8
"""Proveedores de precios para COSTO ESTIMADO. La aplicación no incluye precios.

``PriceTableProvider`` lee una tabla CSV mantenida por el usuario a partir de su
fuente oficial (calculadora de precios de Huawei Cloud, contrato, factura). Columnas:

    service,resource_type,region,match_attribute,match_value,quantity_attribute,unit_price,currency,source

- ``region``: código de región o ``*``.
- ``match_attribute``/``match_value`` (opcionales): filtro sobre ``attributes`` del
  recurso, p. ej. ``flavor_name`` = ``s6.large.2`` o ``volume_type`` = ``GPSSD``.
- ``quantity_attribute`` (opcional): atributo numérico que multiplica el precio,
  p. ej. ``size_gb`` (precio por GB-mes). Vacío = precio fijo por recurso y mes.
- ``unit_price``: importe MENSUAL (por recurso o por unidad), decimal con punto.

Si varias filas aplican gana la más específica: región exacta antes que ``*`` y
con filtro antes que sin filtro. Un recurso sin fila aplicable queda "sin precio"
(nunca se inventa un valor).

Fuente futura (pendiente de validar con una cuenta real): la API de consulta de
precios ``ListOnDemandResourceRatings`` de BSS, que requiere mapear cada recurso a
su ``resource_spec_code`` oficial.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import List, Optional, Protocol, Sequence, Tuple

from costs.report import CostLine, CostReport
from db.models import InventoryResource

ENV_PRICE_TABLE = "INVENTORY_PRICE_TABLE"
REQUIRED_COLUMNS = ("service", "resource_type", "region", "match_attribute", "match_value",
                    "quantity_attribute", "unit_price", "currency", "source")


class PriceTableError(ValueError):
    """Tabla de precios ausente o mal formada (mensaje con fila, sin datos sensibles)."""


class PricingProvider(Protocol):
    name: str

    def price(self, resource: InventoryResource) -> Optional[Tuple[Decimal, str, str]]:
        """``(importe_mensual, moneda, base)`` o ``None`` si no hay precio."""


class NoPricingProvider:
    name = "ninguna"

    def price(self, resource: InventoryResource) -> None:
        return None


@dataclass(frozen=True)
class PriceRule:
    service: str
    resource_type: str
    region: str
    match_attribute: str
    match_value: str
    quantity_attribute: str
    unit_price: Decimal
    currency: str
    source: str

    @property
    def specificity(self) -> Tuple[int, int]:
        return (0 if self.region == "*" else 1, 1 if self.match_attribute else 0)

    def applies(self, resource: InventoryResource) -> bool:
        if (self.service, self.resource_type) != (resource.service, resource.resource_type):
            return False
        if self.region != "*" and self.region != resource.region:
            return False
        if self.match_attribute:
            return str((resource.attributes or {}).get(self.match_attribute, "")) == self.match_value
        return True

    def amount(self, resource: InventoryResource) -> Optional[Decimal]:
        if not self.quantity_attribute:
            return self.unit_price
        raw = (resource.attributes or {}).get(self.quantity_attribute)
        try:
            return self.unit_price * Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            return None


class PriceTableProvider:
    def __init__(self, rules: Sequence[PriceRule], *, name: str = "tabla de precios") -> None:
        self.rules = sorted(rules, key=lambda r: r.specificity, reverse=True)
        self.name = name

    @classmethod
    def from_csv(cls, path: Path) -> "PriceTableProvider":
        if not path.is_file():
            raise PriceTableError(f"No existe la tabla de precios: {path.name}")
        rules: List[PriceRule] = []
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                raise PriceTableError(f"Faltan columnas en la tabla de precios: {', '.join(missing)}")
            for number, row in enumerate(reader, start=2):
                try:
                    price = Decimal(row["unit_price"].strip())
                except (InvalidOperation, AttributeError):
                    raise PriceTableError(f"Fila {number}: unit_price no es un número") from None
                if price < 0 or not row["currency"].strip():
                    raise PriceTableError(f"Fila {number}: precio negativo o moneda vacía")
                rules.append(PriceRule(
                    service=row["service"].strip(), resource_type=row["resource_type"].strip(),
                    region=row["region"].strip(), match_attribute=row["match_attribute"].strip(),
                    match_value=row["match_value"].strip(), quantity_attribute=row["quantity_attribute"].strip(),
                    unit_price=price, currency=row["currency"].strip().upper(), source=row["source"].strip()))
        return cls(rules, name=f"tabla de precios ({path.name}, {len(rules)} reglas)")

    def price(self, resource: InventoryResource) -> Optional[Tuple[Decimal, str, str]]:
        for rule in self.rules:
            if rule.applies(resource):
                amount = rule.amount(resource)
                if amount is None:
                    return None
                basis = f"{rule.unit_price} {rule.currency}/mes" + (f" × {rule.quantity_attribute}" if rule.quantity_attribute else "")
                return amount, rule.currency, f"{basis} ({rule.source or 'sin fuente indicada'})"
        return None


def provider_from_env() -> PricingProvider:
    path = (os.environ.get(ENV_PRICE_TABLE) or "").strip()
    return PriceTableProvider.from_csv(Path(path)) if path else NoPricingProvider()


def estimate(resources: Sequence[InventoryResource], provider: PricingProvider,
             project_names: dict) -> CostReport:
    report = CostReport(kind="estimate", source=provider.name)
    if isinstance(provider, NoPricingProvider):
        report.message = ("No hay fuente de precios configurada: define INVENTORY_PRICE_TABLE con una tabla "
                          "basada en precios oficiales (ver docs/COSTS.md). No se muestran importes inventados.")
    for resource in resources:
        quote = provider.price(resource)
        if quote is None:
            report.unpriced.append({"resource_id": str(resource.id), "service": resource.service,
                                    "resource_type": resource.resource_type, "provider_id": resource.provider_id,
                                    "region": resource.region})
            continue
        amount, currency, basis = quote
        report.lines.append(CostLine(provider_id=resource.provider_id, service=resource.service,
                                     region=resource.region, amount=amount, currency=currency,
                                     resource_id=resource.id, name=resource.name,
                                     project=project_names.get(resource.project_id, "(cuenta)"), basis=basis))
    return report
