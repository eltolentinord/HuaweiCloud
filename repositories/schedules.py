# coding: utf-8
"""Consultas de programaciones de escaneo."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import CloudAccount, ScanRun, ScanSchedule


def list_for_account(session: Session, account_id: uuid.UUID) -> List[ScanSchedule]:
    return list(session.scalars(select(ScanSchedule).where(ScanSchedule.account_id == account_id)
                                .order_by(ScanSchedule.next_run_at, ScanSchedule.id)))


def get_for_client(session: Session, client_id: uuid.UUID, account_id: uuid.UUID,
                   schedule_id: uuid.UUID) -> Optional[ScanSchedule]:
    query = (select(ScanSchedule).join(CloudAccount, CloudAccount.id == ScanSchedule.account_id)
             .where(ScanSchedule.id == schedule_id, ScanSchedule.account_id == account_id,
                    CloudAccount.client_id == client_id))
    return session.scalar(query)


def claim_due(session: Session, *, now: datetime, limit: int = 10) -> List[ScanSchedule]:
    """Programaciones vencidas, BLOQUEADAS para esta transacción.

    En PostgreSQL ``FOR UPDATE SKIP LOCKED``: otro worker concurrente se salta las
    filas ya reclamadas en lugar de esperar o duplicarlas. (SQLite ignora la
    cláusula; allí solo se usa en tests de un único proceso.)
    """
    query = (select(ScanSchedule).where(ScanSchedule.enabled.is_(True), ScanSchedule.next_run_at <= now)
             .order_by(ScanSchedule.next_run_at).limit(limit).with_for_update(skip_locked=True))
    return list(session.scalars(query))


def pending_runs(session: Session, *, limit: int = 10) -> List[ScanRun]:
    """Escaneos en cola (``pending``), los más antiguos primero."""
    return list(session.scalars(select(ScanRun).where(ScanRun.status == "pending")
                                .order_by(ScanRun.created_at, ScanRun.sequence).limit(limit)))


def stale_candidates(session: Session) -> List[ScanRun]:
    return list(session.scalars(select(ScanRun).where(ScanRun.status.in_(("pending", "running")))))
