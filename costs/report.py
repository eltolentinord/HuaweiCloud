# coding: utf-8
"""Líneas de costo y agregación por servicio, región y proyecto (nunca mezcla monedas)."""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional

CENT = Decimal("0.01")


@dataclass
class CostLine:
    """Costo de un recurso (estimado o facturado) en una moneda."""

    provider_id: str
    service: str
    region: str
    amount: Decimal
    currency: str
    resource_id: Optional[uuid.UUID] = None       # recurso del inventario (None si no se encontró)
    project: Optional[str] = None                 # huawei_project_id o "(cuenta)"
    name: Optional[str] = None
    basis: str = ""                               # regla de precio o "facturado"
    official_amount: Optional[Decimal] = None     # precio de lista (solo facturación real)


@dataclass
class CostReport:
    kind: str                                     # "estimate" | "actual"
    source: str                                   # descripción de la fuente
    period: Optional[str] = None                  # YYYY-MM (facturación) o None
    lines: List[CostLine] = field(default_factory=list)
    unpriced: List[Dict[str, Any]] = field(default_factory=list)   # recursos sin precio
    unmatched: List[CostLine] = field(default_factory=list)        # facturado pero no en inventario
    message: Optional[str] = None

    def totals(self) -> Dict[str, Any]:
        """Totales por moneda y desglose por servicio, región y proyecto."""
        groups: Dict[str, Dict[str, Dict[str, Decimal]]] = defaultdict(
            lambda: {"service": defaultdict(Decimal), "region": defaultdict(Decimal),
                     "project": defaultdict(Decimal)})
        total: Dict[str, Decimal] = defaultdict(Decimal)
        for line in self.lines + self.unmatched:
            total[line.currency] += line.amount
            group = groups[line.currency]
            group["service"][line.service] += line.amount
            group["region"][line.region or "(sin región)"] += line.amount
            group["project"][line.project or "(sin proyecto)"] += line.amount

        def money(values: Dict[str, Decimal]) -> Dict[str, str]:
            return {k: str(v.quantize(CENT, ROUND_HALF_UP)) for k, v in sorted(values.items(), key=lambda i: -i[1])}

        return {
            currency: {"total": str(total[currency].quantize(CENT, ROUND_HALF_UP)),
                       "by_service": money(group["service"]), "by_region": money(group["region"]),
                       "by_project": money(group["project"])}
            for currency, group in sorted(groups.items())
        }

    def coverage(self) -> Dict[str, int]:
        return {"priced": len(self.lines), "unpriced": len(self.unpriced), "unmatched_bills": len(self.unmatched)}
