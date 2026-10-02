# coding: utf-8
"""Persistencia de recursos normalizados: upsert por instantánea y borrado lógico.

Identidad de un recurso: ``(account_id, resource_type, scope_key, provider_id)``.

``apply_snapshot`` recibe TODOS los recursos de un servicio en un ámbito
(proyecto o cuenta) devueltos por una consulta del collector y:

- nuevo                          → INSERT (created)
- existente con ``deleted_at``   → se restaura (restored; cuenta como created)
- mismo contenido                → solo ``last_seen``/``last_run_id`` (unchanged)
- contenido distinto             → UPDATE (updated)
- ausente y ``complete=True``    → ``deleted_at = now`` (deleted; nunca DELETE físico)

"Contenido" = ``raw_hash`` + campos normalizados (nombre, estado, región, EP,
fecha de creación, tags, attributes): algunos atributos se derivan de otros
recursos (p. ej. subredes locales de una conexión VPN) y pueden cambiar sin que
cambie su ``raw``.

Con ``complete=False`` (servicio con cobertura parcial) NUNCA se marcan
eliminaciones. Si el collector falló, esta función ni siquiera se llama.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.models import Resource
from core.serialization import fingerprint, parse_provider_datetime, redact_sensitive, redact_values
from db.models import ACCOUNT_SCOPE_KEY, InventoryResource

Key = Tuple[str, str]
_COMPARED_FIELDS = ("name", "status", "region", "enterprise_project_id", "provider_created_at",
                    "tags", "attributes", "raw_hash")


def project_scope_key(project_id: uuid.UUID) -> str:
    return f"project:{project_id}"


def scope_key_for(project_id: Optional[uuid.UUID]) -> str:
    return project_scope_key(project_id) if project_id else ACCOUNT_SCOPE_KEY


@dataclass
class SnapshotStats:
    created: int = 0
    restored: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    skipped_without_id: int = 0
    duplicates: int = 0
    resource_types: Dict[str, int] = field(default_factory=dict)

    @property
    def seen(self) -> int:
        return self.created + self.restored + self.updated + self.unchanged


def _truncate(value: Optional[str], length: int) -> Optional[str]:
    return value[:length] if isinstance(value, str) else value


def _values(resource: Resource, secrets: Sequence[str]) -> Dict[str, Any]:
    """Columnas a guardar, con secretos redactados ANTES de calcular el hash."""
    raw = redact_values(redact_sensitive(resource.raw), secrets)
    return {
        "name": _truncate(redact_values(resource.name, secrets), 512),
        "status": _truncate(resource.status, 64),
        "region": resource.region or "",
        "enterprise_project_id": _truncate(resource.enterprise_project_id, 64),
        "provider_created_at": parse_provider_datetime(resource.created_at),
        "tags": redact_values(redact_sensitive(resource.tags), secrets),
        "attributes": redact_values(redact_sensitive(resource.attributes), secrets),
        "raw": raw,
        "raw_hash": fingerprint(raw),
    }


def _same_instant(a: Optional[datetime], b: Optional[datetime]) -> bool:
    if a is None or b is None:
        return a is b
    if (a.tzinfo is None) != (b.tzinfo is None):  # SQLite devuelve fechas sin zona
        a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
    return a == b


def _changed(row: InventoryResource, values: Dict[str, Any]) -> bool:
    for name in _COMPARED_FIELDS:
        current, new = getattr(row, name), values[name]
        if name == "provider_created_at":
            if not _same_instant(current, new):
                return True
        elif current != new:
            return True
    return False


def index_for_scope(session: Session, account_id: uuid.UUID, scope_key: str,
                    service: str) -> Dict[Key, InventoryResource]:
    """Recursos ya conocidos (incluidos los marcados como eliminados) de un ámbito/servicio."""
    rows = session.scalars(select(InventoryResource).where(
        InventoryResource.account_id == account_id, InventoryResource.scope_key == scope_key,
        InventoryResource.service == service))
    return {(r.resource_type, r.provider_id): r for r in rows}


def apply_snapshot(
    session: Session,
    *,
    account_id: uuid.UUID,
    project_id: Optional[uuid.UUID],
    service: str,
    resources: Iterable[Resource],
    run_id: uuid.UUID,
    now: datetime,
    complete: bool,
    secrets: Sequence[str] = (),
) -> SnapshotStats:
    scope_key = scope_key_for(project_id)
    index = index_for_scope(session, account_id, scope_key, service)
    stats = SnapshotStats()
    seen = set()

    for resource in resources:
        if not resource.provider_id:
            stats.skipped_without_id += 1
            continue
        key = (resource.resource_type, str(resource.provider_id)[:255])
        if key in seen:
            stats.duplicates += 1
            continue
        seen.add(key)
        stats.resource_types[resource.resource_type] = stats.resource_types.get(resource.resource_type, 0) + 1
        values = _values(resource, secrets)
        row = index.get(key)
        if row is None:
            row = InventoryResource(account_id=account_id, project_id=project_id, scope_key=scope_key,
                                    service=service, resource_type=key[0], provider_id=key[1],
                                    first_seen=now, **values)
            session.add(row)
            index[key] = row
            stats.created += 1
        elif row.deleted_at is not None:
            for name, value in values.items():
                setattr(row, name, value)
            row.deleted_at = None
            stats.restored += 1
        elif _changed(row, values):
            for name, value in values.items():
                setattr(row, name, value)
            stats.updated += 1
        else:
            stats.unchanged += 1
        row.last_seen = now
        row.last_run_id = run_id

    if complete:
        for key, row in index.items():
            if key not in seen and row.deleted_at is None:
                row.deleted_at = now
                stats.deleted += 1
    session.flush()
    return stats


def list_for_account(
    session: Session,
    account_id: uuid.UUID,
    *,
    service: Optional[str] = None,
    region: Optional[str] = None,
    resource_type: Optional[str] = None,
    project_id: Optional[uuid.UUID] = None,
    include_deleted: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> Tuple[List[InventoryResource], int]:
    query = select(InventoryResource).where(InventoryResource.account_id == account_id)
    if service:
        query = query.where(InventoryResource.service == service)
    if region:
        query = query.where(InventoryResource.region == region)
    if resource_type:
        query = query.where(InventoryResource.resource_type == resource_type)
    if project_id:
        query = query.where(InventoryResource.project_id == project_id)
    if not include_deleted:
        query = query.where(InventoryResource.deleted_at.is_(None))
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.scalars(query.order_by(InventoryResource.service, InventoryResource.resource_type,
                                          InventoryResource.name, InventoryResource.provider_id)
                           .limit(limit).offset(offset))
    return list(rows), int(total or 0)
