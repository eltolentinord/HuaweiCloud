# coding: utf-8
"""Cobertura de un escaneo por servicio: qué se inventarió completo y qué no (y por qué).

Resume las tareas de un escaneo en un estado por servicio:

- ``succeeded``: todas las tareas terminaron bien (inventario completo).
- ``denied``: todas las tareas sin permiso IAM (401/403 de autorización).
- ``unavailable``: el servicio no existe en esas regiones.
- ``partial``: mezcla (p. ej. permitido en una región y denegado en otra, o paginación cortada).
- ``failed``: alguna tarea falló de verdad (red, API, error interno).
- ``skipped`` / ``pending``: no se ejecutó (todavía).

Para servicios no completos los recursos ya inventariados se conservan: nunca se marcan
como eliminados por falta de permisos (ver ``scanning.engine``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

COMPLETE = {"succeeded", "unavailable"}


@dataclass
class ServiceCoverage:
    service: str
    status: str
    complete: bool
    resources: int = 0
    tasks: Dict[str, int] = field(default_factory=dict)
    regions_affected: List[str] = field(default_factory=list)
    iam_actions: List[str] = field(default_factory=list)
    message: Optional[str] = None


def _summary(statuses: List[str]) -> str:
    unique = set(statuses)
    if len(unique) == 1:
        return statuses[0]
    if "failed" in unique:
        return "failed"
    if unique & {"pending", "running"}:
        return "running"
    if unique <= {"skipped", "unavailable"}:
        return "unavailable" if "unavailable" in unique else "skipped"
    if unique <= {"succeeded", "unavailable"}:
        return "succeeded"
    return "partial"


def service_coverage(tasks: Iterable) -> List[ServiceCoverage]:
    """``tasks``: filas ``ScanTask`` (o equivalentes con los mismos atributos)."""
    by_service: Dict[str, list] = {}
    for task in tasks:
        by_service.setdefault(task.service, []).append(task)

    result = []
    for service in sorted(by_service):
        rows = by_service[service]
        statuses = [t.status for t in rows]
        status = _summary(statuses)
        problems = [t for t in rows if t.status not in COMPLETE]
        counts: Dict[str, int] = {}
        for s in statuses:
            counts[s] = counts.get(s, 0) + 1
        result.append(ServiceCoverage(
            service=service, status=status, complete=all(s in COMPLETE for s in statuses),
            resources=sum(t.resource_count or 0 for t in rows), tasks=counts,
            regions_affected=sorted({t.region for t in problems if t.region}),
            iam_actions=sorted({t.iam_action for t in problems if t.iam_action}),
            message=next((t.error_message_safe for t in problems if t.error_message_safe), None)))
    return result
