# coding: utf-8
"""Comparación entre dos escaneos de una cuenta a partir del historial de eventos.

Identidad: ``resource_id`` (= cuenta + tipo + ámbito + ``provider_id``), nunca el nombre.

Para cada recurso con eventos en la ventana ``(A, B]`` se reproduce la secuencia:
- ¿existía en A?  lo dice el PRIMER evento de la ventana: ``created``/``restored``
  implican que no existía; ``updated``/``deleted`` implican que sí.
- ¿existe en B?   lo dice el ÚLTIMO evento: todo menos ``deleted``.

    antes  después  →  categoría
    no     sí          added
    sí     no          removed
    sí     sí          modified   (cambios de campos fusionados: primer "antes", último "después")
    no     no          transient  (apareció y desapareció entre A y B)

Los escaneos intermedios cuentan (un cambio visto en un escaneo intermedio forma
parte de la diferencia A→B). Los servicios que fallaron no generan eventos, por lo
que nunca aparecen como eliminaciones falsas.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import ResourceChange, ScanRun
from repositories import changes as changes_repo
from repositories import scans as scans_repo
from tenancy.errors import NotFoundError, ValidationFailedError

CATEGORIES = ("added", "removed", "modified", "transient")


@dataclass
class ResourceDiff:
    resource_id: uuid.UUID
    category: str
    service: str
    resource_type: str
    provider_id: str
    region: str
    name: Optional[str]
    events: List[str] = field(default_factory=list)
    changed_fields: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ScanComparison:
    from_run: ScanRun
    to_run: ScanRun
    intermediate_runs: int
    items: List[ResourceDiff]
    swapped: bool = False

    def summary(self) -> Dict[str, Any]:
        counts = {c: 0 for c in CATEGORIES}
        by_service: Dict[str, Dict[str, int]] = {}
        for item in self.items:
            counts[item.category] += 1
            service = by_service.setdefault(item.service, {c: 0 for c in CATEGORIES})
            service[item.category] += 1
        return {"counts": counts, "by_service": dict(sorted(by_service.items())),
                "intermediate_runs": self.intermediate_runs, "swapped": self.swapped}


def _merge_fields(events: List[ResourceChange]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for event in events:
        for change in event.changed_fields or []:
            name = change.get("field")
            if name in merged:
                merged[name]["after"] = change.get("after")
            else:
                merged[name] = {"field": name, "before": change.get("before"), "after": change.get("after")}
    return [c for c in merged.values() if c["before"] != c["after"]]


def diff_events(events: List[ResourceChange]) -> List[ResourceDiff]:
    """Clasifica los eventos (ya en orden cronológico) por recurso."""
    grouped: Dict[uuid.UUID, List[ResourceChange]] = {}
    for event in events:
        grouped.setdefault(event.resource_id, []).append(event)
    result = []
    for resource_id, items in grouped.items():
        existed_before = items[0].change_type in ("updated", "deleted")
        exists_after = items[-1].change_type != "deleted"
        category = {(False, True): "added", (True, False): "removed",
                    (True, True): "modified", (False, False): "transient"}[(existed_before, exists_after)]
        last = items[-1]
        result.append(ResourceDiff(
            resource_id=resource_id, category=category, service=last.service,
            resource_type=last.resource_type, provider_id=last.provider_id, region=last.region,
            name=last.name, events=[e.change_type for e in items],
            changed_fields=_merge_fields(items) if category == "modified" else []))
    result.sort(key=lambda d: (CATEGORIES.index(d.category), d.service, d.resource_type, d.provider_id))
    return result


def compare_scans(session: Session, *, client_id: uuid.UUID, account_id: uuid.UUID,
                  from_scan: uuid.UUID, to_scan: uuid.UUID) -> ScanComparison:
    first = scans_repo.get_for_client(session, client_id, from_scan)
    second = scans_repo.get_for_client(session, client_id, to_scan)
    if first is None or second is None or first.account_id != account_id or second.account_id != account_id:
        raise NotFoundError("Escaneo no encontrado para esta cuenta.")
    swapped = first.sequence > second.sequence
    if swapped:
        first, second = second, first
    if first.id == second.id:
        raise ValidationFailedError("Indica dos escaneos distintos.")
    window = list(session.scalars(select(ScanRun.id).where(
        ScanRun.account_id == account_id, ScanRun.sequence > first.sequence,
        ScanRun.sequence <= second.sequence)))
    events = changes_repo.between_runs(session, account_id, window)
    return ScanComparison(from_run=first, to_run=second, intermediate_runs=max(len(window) - 1, 0),
                          items=diff_events(events), swapped=swapped)
