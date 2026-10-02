# coding: utf-8
"""Consultas del historial de cambios (``resource_changes``)."""

from __future__ import annotations

import uuid
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import ResourceChange, ScanRun


def for_run(session: Session, run_id: uuid.UUID, *, change_type: Optional[str] = None,
            service: Optional[str] = None, limit: int = 100,
            offset: int = 0) -> Tuple[List[ResourceChange], int]:
    query = select(ResourceChange).where(ResourceChange.scan_run_id == run_id)
    if change_type:
        query = query.where(ResourceChange.change_type == change_type)
    if service:
        query = query.where(ResourceChange.service == service)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.scalars(query.order_by(ResourceChange.service, ResourceChange.resource_type,
                                          ResourceChange.provider_id).limit(limit).offset(offset))
    return list(rows), int(total or 0)


def counts_for_run(session: Session, run_id: uuid.UUID) -> dict:
    rows = session.execute(select(ResourceChange.change_type, func.count())
                           .where(ResourceChange.scan_run_id == run_id)
                           .group_by(ResourceChange.change_type))
    return {change_type: count for change_type, count in rows}


def for_resource(session: Session, resource_id: uuid.UUID, *, limit: int = 50,
                 offset: int = 0) -> Tuple[List[ResourceChange], int]:
    query = select(ResourceChange).where(ResourceChange.resource_id == resource_id)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.scalars(query.join(ScanRun, ScanRun.id == ResourceChange.scan_run_id)
                           .order_by(ScanRun.sequence.desc(), ResourceChange.created_at.desc())
                           .limit(limit).offset(offset))
    return list(rows), int(total or 0)


def between_runs(session: Session, account_id: uuid.UUID, run_ids: Sequence[uuid.UUID]) -> List[ResourceChange]:
    """Eventos de la cuenta producidos por los escaneos indicados, en orden cronológico."""
    if not run_ids:
        return []
    query = (select(ResourceChange).join(ScanRun, ScanRun.id == ResourceChange.scan_run_id)
             .where(ResourceChange.account_id == account_id, ResourceChange.scan_run_id.in_(list(run_ids)))
             .order_by(ScanRun.sequence, ResourceChange.created_at, ResourceChange.id))
    return list(session.scalars(query))
