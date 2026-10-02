# coding: utf-8
"""CostComparison: totales de origen y destino y sus diferencias.

Reglas (sin estimaciones silenciosas):
- Un total solo existe si TODOS sus componentes tienen precio y en UNA sola moneda.
- La diferencia solo se calcula si ambos totales existen y comparten moneda.
- Diferencia absoluta = destino − origen.
- Diferencia porcentual = (destino − origen) / origen × 100 (sin valor si origen = 0).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple

from costs.resolver import PricedComponent

CENT = Decimal("0.01")


def money(value: Optional[Decimal]) -> Optional[str]:
    return str(value.quantize(CENT, ROUND_HALF_UP)) if value is not None else None


def difference(origin: Optional[Decimal], destination: Optional[Decimal]) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    """``(absoluta, porcentual)``; ``None`` cuando no se puede calcular."""
    if origin is None or destination is None:
        return None, None
    absolute = destination - origin
    percent = None if origin == 0 else (absolute / origin * 100).quantize(CENT, ROUND_HALF_UP)
    return absolute, percent


@dataclass
class SideCost:
    lines: List[PricedComponent] = field(default_factory=list)
    total: Optional[Decimal] = None
    currency: Optional[str] = None
    missing: List[str] = field(default_factory=list)
    reason: Optional[str] = None

    @classmethod
    def from_lines(cls, lines: List[PricedComponent]) -> "SideCost":
        side = cls(lines=lines, missing=[l.component.label for l in lines if not l.available])
        currencies = {l.currency for l in lines if l.available}
        if side.missing:
            side.reason = "Precio no disponible para: " + ", ".join(side.missing)
        elif len(currencies) > 1:
            side.reason = "Los componentes tienen monedas distintas; no se suman."
        elif lines:
            side.currency = currencies.pop()
            side.total = sum((l.monthly_amount for l in lines if l.monthly_amount is not None), Decimal("0"))
        else:
            side.reason = "Sin componentes con precio."
        return side

    @property
    def updated_at(self) -> Optional[str]:
        dates = [l.updated_at for l in self.lines if l.updated_at]
        return min(dates).isoformat() if dates else None   # el precio más antiguo usado

    @property
    def sources(self) -> List[str]:
        return sorted({l.source for l in self.lines if l.source})

    def as_dict(self) -> Dict[str, Any]:
        return {"total": money(self.total), "currency": self.currency, "complete": self.total is not None,
                "reason": self.reason, "sources": self.sources, "updated_at": self.updated_at,
                "lines": [l.as_dict() for l in self.lines]}


@dataclass
class CostComparison:
    origin: SideCost
    destination: SideCost

    @property
    def comparable(self) -> bool:
        return (self.origin.total is not None and self.destination.total is not None
                and self.origin.currency == self.destination.currency)

    def deltas(self) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        if not self.comparable:
            return None, None
        return difference(self.origin.total, self.destination.total)

    @property
    def reason(self) -> Optional[str]:
        if self.comparable:
            return None
        if self.origin.total is None or self.destination.total is None:
            return "No se puede comparar: falta algún precio (ver detalle)."
        return "No se puede comparar: origen y destino usan monedas distintas."

    def as_dict(self) -> Dict[str, Any]:
        absolute, percent = self.deltas()
        return {"origin": self.origin.as_dict(), "destination": self.destination.as_dict(),
                "comparable": self.comparable, "reason": self.reason,
                "difference": money(absolute), "difference_percent": str(percent) if percent is not None else None,
                "currency": self.origin.currency if self.comparable else None}
