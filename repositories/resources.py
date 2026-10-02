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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.models import Resource
from core.serialization import (
    canonical_json,
    fingerprint,
    parse_provider_datetime,
    redact_sensitive,
    redact_values,
)
from db.models import ACCOUNT_SCOPE_KEY, InventoryResource, ResourceChange

Key = Tuple[str, str]
_SCALAR_FIELDS = ("name", "status", "region", "enterprise_project_id")
_MAPPING_FIELDS = ("tags", "attributes")
MAX_CHANGED_FIELDS = 50
MAX_VALUE_LENGTH = 300


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


def _short(value: Any) -> Any:
    """Valor compacto para el historial (ya redactado): textos y JSON truncados."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    text = value if isinstance(value, str) else canonical_json(value)
    if len(text) <= MAX_VALUE_LENGTH:
        return value
    return text[:MAX_VALUE_LENGTH] + "…"


def field_diff(row: InventoryResource, values: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Campos normalizados que cambian (``attributes.x``, ``tags.y``...) con antes/después.

    Un cambio que solo afecta a ``raw`` se registra como ``{"field": "raw"}`` con los
    hashes abreviados: el contenido de ``raw`` no se duplica en el historial.
    """
    changes: List[Dict[str, Any]] = []

    def add(name: str, before: Any, after: Any) -> None:
        changes.append({"field": name, "before": _short(before), "after": _short(after)})

    for name in _SCALAR_FIELDS:
        if getattr(row, name) != values[name]:
            add(name, getattr(row, name), values[name])
    if not _same_instant(row.provider_created_at, values["provider_created_at"]):
        add("provider_created_at", row.provider_created_at, values["provider_created_at"])
    for mapping in _MAPPING_FIELDS:
        old, new = getattr(row, mapping) or {}, values[mapping] or {}
        for key in sorted(set(old) | set(new)):
            if old.get(key) != new.get(key):
                add(f"{mapping}.{key}", old.get(key), new.get(key))
    if row.raw_hash != values["raw_hash"]:
        add("raw", (row.raw_hash or "")[:12], values["raw_hash"][:12])
    return changes[:MAX_CHANGED_FIELDS]


def _event(row: InventoryResource, run_id: uuid.UUID, change_type: str, *, changed_fields=(),
           raw_hash_before: Optional[str] = None) -> ResourceChange:
    return ResourceChange(account_id=row.account_id, resource_id=row.id, scan_run_id=run_id,
                          change_type=change_type, service=row.service, resource_type=row.resource_type,
                          provider_id=row.provider_id, region=row.region or "", name=row.name,
                          changed_fields=list(changed_fields), raw_hash_before=raw_hash_before,
                          raw_hash_after=row.raw_hash if change_type != "deleted" else None)


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
    events: List[ResourceChange] = []

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
            row = InventoryResource(id=uuid.uuid4(), account_id=account_id, project_id=project_id,
                                    scope_key=scope_key, service=service, resource_type=key[0],
                                    provider_id=key[1], first_seen=now, **values)
            session.add(row)
            index[key] = row
            stats.created += 1
            events.append(_event(row, run_id, "created"))
        elif row.deleted_at is not None:
            diff, before = field_diff(row, values), row.raw_hash
            for name, value in values.items():
                setattr(row, name, value)
            row.deleted_at = None
            stats.restored += 1
            events.append(_event(row, run_id, "restored", changed_fields=diff, raw_hash_before=before))
        else:
            diff = field_diff(row, values)
            if diff:
                before = row.raw_hash
                for name, value in values.items():
                    setattr(row, name, value)
                stats.updated += 1
                events.append(_event(row, run_id, "updated", changed_fields=diff, raw_hash_before=before))
            else:
                stats.unchanged += 1
        row.last_seen = now
        row.last_run_id = run_id

    if complete:
        for key, row in index.items():
            if key not in seen and row.deleted_at is None:
                row.deleted_at = now
                stats.deleted += 1
                events.append(_event(row, run_id, "deleted", raw_hash_before=row.raw_hash))
    session.flush()  # los recursos nuevos deben existir antes que sus eventos (FK)
    session.add_all(events)
    session.flush()
    return stats


# Ordenaciones permitidas (lista blanca: el parámetro del usuario nunca llega al SQL).
SORTABLE_FIELDS = {
    "name": InventoryResource.name,
    "service": InventoryResource.service,
    "resource_type": InventoryResource.resource_type,
    "region": InventoryResource.region,
    "status": InventoryResource.status,
    "provider_id": InventoryResource.provider_id,
    "first_seen": InventoryResource.first_seen,
    "last_seen": InventoryResource.last_seen,
    "provider_created_at": InventoryResource.provider_created_at,
    "deleted_at": InventoryResource.deleted_at,
}
DEFAULT_SORT = ("service", "resource_type", "name", "provider_id")


def parse_sort(sort: Optional[str]) -> List[Tuple[str, bool]]:
    """``"-last_seen,name"`` → ``[("last_seen", desc), ("name", asc)]``. Lanza ``ValueError``."""
    if not sort:
        return [(name, False) for name in DEFAULT_SORT]
    result = []
    for part in (p.strip() for p in sort.split(",") if p.strip()):
        descending = part.startswith("-")
        name = part.lstrip("+-")
        if name not in SORTABLE_FIELDS:
            raise ValueError(f"No se puede ordenar por '{name}'. Opciones: {', '.join(sorted(SORTABLE_FIELDS))}")
        result.append((name, descending))
    return result


def list_for_account(
    session: Session,
    account_id: uuid.UUID,
    *,
    service: Optional[str] = None,
    region: Optional[str] = None,
    resource_type: Optional[str] = None,
    project_id: Optional[uuid.UUID] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    include_deleted: bool = False,
    only_deleted: bool = False,
    sort: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> Tuple[List[InventoryResource], int]:
    query = _filtered(account_id, service=service, region=region, resource_type=resource_type,
                      project_id=project_id, status=status, search=search,
                      include_deleted=include_deleted, only_deleted=only_deleted)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    order: List[Any] = []
    for name, descending in parse_sort(sort):
        column = SORTABLE_FIELDS[name]
        order.append(column.desc().nulls_last() if descending else column.asc().nulls_last())
    order.append(InventoryResource.id)  # orden total estable para paginar
    rows = session.scalars(query.order_by(*order).limit(limit).offset(offset))
    return list(rows), int(total or 0)


def _filtered(account_id: uuid.UUID, *, service=None, region=None, resource_type=None, project_id=None,
              status=None, search=None, include_deleted=False, only_deleted=False):
    query = select(InventoryResource).where(InventoryResource.account_id == account_id)
    if service:
        query = query.where(InventoryResource.service == service)
    if region:
        query = query.where(InventoryResource.region == region)
    if resource_type:
        query = query.where(InventoryResource.resource_type == resource_type)
    if project_id:
        query = query.where(InventoryResource.project_id == project_id)
    if status:
        query = query.where(func.lower(InventoryResource.status) == status.lower())
    if search:
        term = search.strip().lower()
        query = query.where(
            func.lower(InventoryResource.name).contains(term, autoescape=True)
            | func.lower(InventoryResource.provider_id).contains(term, autoescape=True))
    if only_deleted:
        query = query.where(InventoryResource.deleted_at.is_not(None))
    elif not include_deleted:
        query = query.where(InventoryResource.deleted_at.is_(None))
    return query


def get_for_account(session: Session, account_id: uuid.UUID, resource_id: uuid.UUID) -> Optional[InventoryResource]:
    return session.scalar(select(InventoryResource).where(InventoryResource.id == resource_id,
                                                           InventoryResource.account_id == account_id))


def _group_count(session: Session, account_id: uuid.UUID, column, *, deleted: bool = False) -> Dict[str, int]:
    condition = InventoryResource.deleted_at.is_not(None) if deleted else InventoryResource.deleted_at.is_(None)
    rows = session.execute(select(column, func.count()).where(InventoryResource.account_id == account_id, condition)
                           .group_by(column).order_by(column))
    return {str(key) if key is not None else "": int(cast(int, count)) for key, count in rows}


def stats_for_account(session: Session, account_id: uuid.UUID) -> Dict[str, Any]:
    """Inventario activo agrupado (servicio, región, tipo, proyecto, estado) y eliminados."""
    by_service = _group_count(session, account_id, InventoryResource.service)
    return {
        "total_active": sum(by_service.values()),
        "total_deleted": sum(_group_count(session, account_id, InventoryResource.service, deleted=True).values()),
        "by_service": by_service,
        "by_region": _group_count(session, account_id, InventoryResource.region),
        "by_resource_type": _group_count(session, account_id, InventoryResource.resource_type),
        "by_project": _group_count(session, account_id, InventoryResource.project_id),
        "by_status": _group_count(session, account_id, InventoryResource.status),
    }
