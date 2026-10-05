# coding: utf-8
"""Registro de auditoría de acciones administrativas (API, CLI y worker).

Uso: ``record(session, actor, "account.create", client_id=..., account_id=...,
target=("account", account.id), details={"name": ...})``.

Seguridad: ``details`` pasa por una lista blanca de claves y por la redacción de
claves sensibles; nunca se registran AK/SK, tokens ni cuerpos de petición.
"""

from __future__ import annotations

import getpass
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.authz import Principal
from core.observability import request_id_var
from core.serialization import redact_sensitive
from db.models import AuditEvent

ALLOWED_DETAIL_KEYS = frozenset({
    "name", "slug", "status", "services", "regions", "project_ids", "interval_minutes", "enabled",
    "huawei_project_id", "region_id", "is_enabled", "key_version", "fields", "created", "updated",
    "skipped", "trigger", "endpoint_domain", "scan_sequence",
})


def _clean(details: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    safe = {k: v for k, v in (details or {}).items() if k in ALLOWED_DETAIL_KEYS}
    return redact_sensitive(safe)


_clock_lock = threading.Lock()
_last_timestamp: Optional[datetime] = None
_TICK = timedelta(microseconds=1)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def next_timestamp() -> datetime:
    """Marca de tiempo ESTRICTAMENTE creciente dentro del proceso.

    ``datetime.now()`` puede devolver el mismo valor en llamadas seguidas (en Windows
    su resolución es de milisegundos) o retroceder si se ajusta el reloj. Con valores
    repetidos el orden del registro dependería del ``id`` aleatorio. Aquí cada evento
    recibe al menos 1 µs más que el anterior (PostgreSQL y SQLite guardan µs), de modo
    que el orden de ``occurred_at`` es el orden real en que el proceso registró los
    eventos. Entre procesos distintos, eventos del mismo µs son concurrentes y se
    desempatan de forma estable por ``id``.
    """
    global _last_timestamp
    with _clock_lock:
        now = _utcnow()
        if _last_timestamp is not None and now <= _last_timestamp:
            now = _last_timestamp + _TICK
        _last_timestamp = now
        return now


def cli_principal() -> Principal:
    try:
        user = getpass.getuser()
    except Exception:  # entornos sin usuario identificable
        user = "desconocido"
    return Principal(subject=f"cli:{user}", kind="cli", platform_role="admin")


WORKER_PRINCIPAL = Principal(subject="worker", kind="worker", platform_role="admin")


def record(session: Session, actor: Principal, action: str, *, client_id: Optional[uuid.UUID] = None,
           account_id: Optional[uuid.UUID] = None, target: Optional[Tuple[str, Any]] = None,
           details: Optional[Dict[str, Any]] = None) -> AuditEvent:
    # Marca de tiempo monótona desde Python (no now() de la base, que puede repetirse):
    # el orden del registro es el orden de los eventos.
    event = AuditEvent(occurred_at=next_timestamp(), actor_subject=actor.subject[:200],
                       actor_kind=actor.kind[:20], client_id=client_id, account_id=account_id, action=action[:64],
                       target_type=target[0][:32] if target else None,
                       target_id=str(target[1])[:64] if target else None,
                       request_id=request_id_var.get(), details=_clean(details))
    session.add(event)
    session.flush()
    return event


def list_for_client(session: Session, client_id: uuid.UUID, *, action: Optional[str] = None,
                    limit: int = 100, offset: int = 0) -> Tuple[List[AuditEvent], int]:
    query = select(AuditEvent).where(AuditEvent.client_id == client_id)
    if action:
        query = query.where(AuditEvent.action == action)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.scalars(query.order_by(AuditEvent.occurred_at.desc(), AuditEvent.id).limit(limit).offset(offset))
    return list(rows), int(total or 0)
