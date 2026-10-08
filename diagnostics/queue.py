# coding: utf-8
"""Cola de diagnósticos en base de datos.

Usa FOR UPDATE SKIP LOCKED para que varios workers no procesen el mismo incidente.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from db.models import DiagnosticIncident, IN_PROGRESS_STATUSES

CLAIMABLE_STATUSES = ("alert_received", "pending_diagnosis")
MAX_CLAIM = 5


def claim_pending(session: Session, *, limit: int = MAX_CLAIM) -> List[DiagnosticIncident]:
    """Reclama incidentes pendientes de forma atómica (FOR UPDATE SKIP LOCKED)."""
    stmt = (
        select(DiagnosticIncident)
        .where(DiagnosticIncident.status.in_(CLAIMABLE_STATUSES))
        .order_by(DiagnosticIncident.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(session.scalars(stmt))


def set_status(session: Session, incident_id: uuid.UUID, status: str, *,
               error_kind: Optional[str] = None,
               error_message_safe: Optional[str] = None,
               duration_ms: Optional[int] = None) -> None:
    values: dict = {"status": status}
    if error_kind is not None:
        values["error_kind"] = error_kind
    if error_message_safe is not None:
        values["error_message_safe"] = error_message_safe
    if duration_ms is not None:
        values["duration_ms"] = duration_ms
    session.execute(
        update(DiagnosticIncident)
        .where(DiagnosticIncident.id == incident_id)
        .values(**values)
    )
