# coding: utf-8
"""Retención de datos: limita el crecimiento del historial sin romper su coherencia.

- Escaneos: se eliminan los TERMINADOS más antiguos que ``keep_days``, conservando
  siempre los ``keep_min`` más recientes de cada cuenta y nunca los activos
  (``pending``/``running``). Se borra el escaneo completo: sus tareas y sus eventos
  de cambio se van en cascada, así la comparación entre los escaneos que quedan
  sigue siendo coherente (sus ventanas de eventos están intactas).
- Recursos eliminados (``deleted_at``): opcionalmente se purgan los que llevan más
  de ``purge_deleted_days`` desaparecidos (con su historial).
- Por defecto es una SIMULACIÓN: solo cuenta lo que se borraría.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from db.models import InventoryResource, ResourceChange, ScanRun, ScanSchedule, ScanTask

TERMINAL_STATUSES = ("completed", "completed_with_warnings", "completed_with_errors", "failed")


@dataclass
class PruneReport:
    applied: bool
    scans: int = 0
    scan_changes: int = 0
    deleted_resources: int = 0
    resource_changes: int = 0

    def as_dict(self) -> dict:
        return {"applied": self.applied, "scans": self.scans, "scan_changes": self.scan_changes,
                "deleted_resources": self.deleted_resources, "resource_changes": self.resource_changes}


def _old_scan_ids(session: Session, *, cutoff: datetime, keep_min: int) -> List:
    ranked = (select(ScanRun.id, ScanRun.status, ScanRun.created_at,
                     func.row_number().over(partition_by=ScanRun.account_id,
                                            order_by=ScanRun.sequence.desc()).label("rank"))
              .subquery())
    query = select(ranked.c.id).where(ranked.c.rank > keep_min, ranked.c.status.in_(TERMINAL_STATUSES),
                                      ranked.c.created_at < cutoff)
    return list(session.scalars(query))


def prune(session: Session, *, keep_days: int = 180, keep_min: int = 20,
          purge_deleted_days: Optional[int] = None, apply: bool = False,
          now: Optional[datetime] = None) -> PruneReport:
    if keep_days < 1 or keep_min < 1:
        raise ValueError("keep_days y keep_min deben ser al menos 1.")
    now = now or datetime.now(timezone.utc)
    report = PruneReport(applied=apply)

    scan_ids = _old_scan_ids(session, cutoff=now - timedelta(days=keep_days), keep_min=keep_min)
    report.scans = len(scan_ids)
    if scan_ids:
        report.scan_changes = int(session.scalar(select(func.count()).select_from(ResourceChange)
                                                 .where(ResourceChange.scan_run_id.in_(scan_ids))) or 0)

    resource_ids: List = []
    if purge_deleted_days is not None:
        if purge_deleted_days < 1:
            raise ValueError("purge_deleted_days debe ser al menos 1.")
        resource_ids = list(session.scalars(select(InventoryResource.id).where(
            InventoryResource.deleted_at.is_not(None),
            InventoryResource.deleted_at < now - timedelta(days=purge_deleted_days))))
        report.deleted_resources = len(resource_ids)
        if resource_ids:
            report.resource_changes = int(session.scalar(select(func.count()).select_from(ResourceChange)
                                                         .where(ResourceChange.resource_id.in_(resource_ids))) or 0)

    if apply:
        # Dependientes explícitos: no se confía en ON DELETE (en SQLite depende del PRAGMA).
        if scan_ids:
            session.execute(delete(ResourceChange).where(ResourceChange.scan_run_id.in_(scan_ids)))
            session.execute(delete(ScanTask).where(ScanTask.scan_run_id.in_(scan_ids)))
            for model in (InventoryResource, ScanSchedule):
                session.execute(update(model).where(model.last_run_id.in_(scan_ids))
                                .values(last_run_id=None).execution_options(synchronize_session=False))
            session.execute(delete(ScanRun).where(ScanRun.id.in_(scan_ids)))
        if resource_ids:
            session.execute(delete(ResourceChange).where(ResourceChange.resource_id.in_(resource_ids)))
            session.execute(delete(InventoryResource).where(InventoryResource.id.in_(resource_ids)))
        session.flush()
    return report
