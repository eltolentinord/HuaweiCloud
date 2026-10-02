# coding: utf-8
"""Consultas de ejecuciones (ScanRun) y tareas (ScanTask)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from db.models import CloudAccount, ScanRun, ScanTask

ACTIVE_STATUSES = ("pending", "running")


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def get(session: Session, run_id: uuid.UUID) -> Optional[ScanRun]:
    return session.get(ScanRun, run_id)


def get_for_client(session: Session, client_id: uuid.UUID, run_id: uuid.UUID) -> Optional[ScanRun]:
    query = (select(ScanRun).join(CloudAccount, CloudAccount.id == ScanRun.account_id)
             .where(ScanRun.id == run_id, CloudAccount.client_id == client_id))
    return session.scalar(query)


def list_for_account(session: Session, account_id: uuid.UUID, *, limit: int = 20) -> List[ScanRun]:
    return list(session.scalars(select(ScanRun).where(ScanRun.account_id == account_id)
                                .order_by(ScanRun.created_at.desc()).limit(limit)))


def active_for_account(session: Session, account_id: uuid.UUID) -> Optional[ScanRun]:
    """Ejecución ``pending``/``running`` de la cuenta (como mucho una: índice único parcial)."""
    return session.scalar(select(ScanRun).where(
        ScanRun.account_id == account_id, ScanRun.status.in_(ACTIVE_STATUSES)).limit(1))


def is_stale(run: ScanRun, *, now: datetime, max_silence) -> bool:
    """Sin latido (``updated_at``) durante ``max_silence``: proceso caído o reiniciado.

    Se compara en Python, siempre en UTC (SQLite devuelve fechas sin zona).
    """
    last_beat = run.updated_at or run.created_at
    return last_beat is not None and _utc(last_beat) < now - max_silence


def claim_pending(session: Session, run_id: uuid.UUID, *, now: datetime) -> bool:
    """Pasa ``pending → running`` de forma ATÓMICA. ``False`` si otro proceso ya lo tomó."""
    result = session.execute(
        update(ScanRun).where(ScanRun.id == run_id, ScanRun.status == "pending")
        .values(status="running", started_at=now, updated_at=now)
        .execution_options(synchronize_session=False))
    return result.rowcount == 1


def heartbeat(session: Session, run_id: uuid.UUID, *, now: datetime) -> None:
    session.execute(update(ScanRun).where(ScanRun.id == run_id).values(updated_at=now)
                    .execution_options(synchronize_session=False))


def tasks(session: Session, run_id: uuid.UUID) -> List[ScanTask]:
    return list(session.scalars(select(ScanTask).where(ScanTask.scan_run_id == run_id)
                                .order_by(ScanTask.sequence)))
