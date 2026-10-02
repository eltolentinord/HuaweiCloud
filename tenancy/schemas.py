# coding: utf-8
"""Esquemas de entrada/salida de la API de administración.

Los modelos de salida NO tienen campos de AK/SK ni de ciphertext: es imposible
serializarlos por error. En la entrada se usa ``SecretStr`` (repr enmascarado).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: Optional[str] = Field(default=None, max_length=80)
    status: str = "active"


class ClientPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    slug: Optional[str] = Field(default=None, max_length=80)
    status: Optional[str] = None


class ClientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    status: str
    created_at: datetime
    updated_at: datetime


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    ak: SecretStr = Field(min_length=1, max_length=256)
    sk: SecretStr = Field(min_length=1, max_length=512)
    huawei_domain_id: Optional[str] = Field(default=None, max_length=64)
    huawei_domain_name: Optional[str] = Field(default=None, max_length=200)
    endpoint_domain: str = Field(default="myhuaweicloud.com", max_length=255)
    iam_region_id: Optional[str] = Field(default=None, max_length=64)


class AccountPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    status: Optional[str] = None
    huawei_domain_id: Optional[str] = Field(default=None, max_length=64)
    huawei_domain_name: Optional[str] = Field(default=None, max_length=200)
    endpoint_domain: Optional[str] = Field(default=None, max_length=255)
    iam_region_id: Optional[str] = Field(default=None, max_length=64)


class CredentialsIn(BaseModel):
    ak: SecretStr = Field(min_length=1, max_length=256)
    sk: SecretStr = Field(min_length=1, max_length=512)


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    client_id: uuid.UUID
    name: str
    huawei_domain_id: Optional[str]
    huawei_domain_name: Optional[str]
    endpoint_domain: str
    iam_region_id: Optional[str]
    key_version: int
    status: str
    last_validated_at: Optional[datetime]
    last_validation_error: Optional[str]
    created_at: datetime
    updated_at: datetime


class ProjectIn(BaseModel):
    huawei_project_id: str = Field(min_length=1, max_length=64)
    region_id: str = Field(min_length=1, max_length=64)
    name: Optional[str] = Field(default=None, max_length=200)


class ProjectPatch(BaseModel):
    is_enabled: bool


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    huawei_project_id: str
    region_id: str
    name: Optional[str]
    parent_huawei_project_id: Optional[str]
    is_enabled: bool
    huawei_enabled: Optional[bool]
    discovered_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class DiscoveryOut(BaseModel):
    created: int
    updated: int
    unchanged: int
    skipped: List[dict]
    missing: List[str]
    projects: List[ProjectOut]


class AccountInventoryIn(BaseModel):
    project_id: uuid.UUID
    service: str = "ecs"


# ---------------------------------------------------------------- Fase 3A: escaneos
class ScanIn(BaseModel):
    services: Optional[List[str]] = Field(default=None, description="Por defecto, todos los habilitados")
    regions: Optional[List[str]] = Field(default=None, description="Por defecto, todas las de la cuenta")
    project_ids: Optional[List[uuid.UUID]] = Field(default=None, description="IDs internos de proyectos")


class ScanProgress(BaseModel):
    total: int
    done: int
    percent: float


class ScanRunSummary(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    sequence: int
    status: str
    trigger: str
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    duration_ms: Optional[int]
    total_tasks: int
    total_resources: int
    total_created: int
    total_updated: int
    total_deleted: int
    total_errors: int
    total_warnings: int
    error_message_safe: Optional[str]
    created_at: Optional[datetime]

    @classmethod
    def from_run(cls, run) -> "ScanRunSummary":
        return cls(**{name: getattr(run, name) for name in cls.model_fields})


class ScanTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sequence: int
    project_id: Optional[uuid.UUID]
    service: str
    scope: str
    region: str
    status: str
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    duration_ms: Optional[int]
    resource_count: int
    created_count: int
    updated_count: int
    deleted_count: int
    error_count: int
    error_kind: Optional[str]
    iam_action: Optional[str]
    http_status: Optional[int]
    error_code: Optional[str]
    request_id: Optional[str]
    error_message_safe: Optional[str]
    notices: List[str]


class ScanRunOut(ScanRunSummary):
    progress: ScanProgress
    stats: Dict[str, Any]
    errors: List[ScanTaskOut]     # fallos reales (failed/skipped)
    warnings: List[ScanTaskOut]   # avisos: denied (401/403 de permisos), unavailable, partial
    tasks: List[ScanTaskOut]

    @classmethod
    def from_run(cls, run, tasks=()) -> "ScanRunOut":  # type: ignore[override]
        tasks = [ScanTaskOut.model_validate(t) for t in tasks]
        done = sum(t.status not in ("pending", "running") for t in tasks)
        summary = ScanRunSummary.from_run(run).model_dump()
        return cls(**summary, stats=run.stats or {}, tasks=tasks,
                   errors=[t for t in tasks if t.status in ("failed", "skipped")],
                   warnings=[t for t in tasks if t.status in ("denied", "unavailable", "partial")],
                   progress=ScanProgress(total=len(tasks), done=done,
                                         percent=round(100 * done / len(tasks), 1) if tasks else 100.0))


class ResourceOut(BaseModel):
    id: uuid.UUID
    project_id: Optional[uuid.UUID]
    region: str
    service: str
    resource_type: str
    provider_id: str
    name: Optional[str]
    status: Optional[str]
    enterprise_project_id: Optional[str]
    provider_created_at: Optional[datetime]
    tags: Dict[str, Any]
    attributes: Dict[str, Any]
    raw_hash: str
    first_seen: datetime
    last_seen: datetime
    deleted_at: Optional[datetime]
    last_run_id: Optional[uuid.UUID]
    raw: Optional[Dict[str, Any]] = None

    @classmethod
    def from_row(cls, row, *, include_raw: bool = False) -> "ResourceOut":
        data = {name: getattr(row, name) for name in cls.model_fields if name != "raw"}
        return cls(**data, raw=row.raw if include_raw else None)


class ResourcePage(BaseModel):
    total: int
    limit: int
    offset: int
    items: List[ResourceOut]


# ---------------------------------------------------- Fase 4: inventario e historial
class Page(BaseModel):
    """Envoltorio común de listas paginadas."""

    total: int
    limit: int
    offset: int


class ChangeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    resource_id: uuid.UUID
    scan_run_id: uuid.UUID
    change_type: str
    service: str
    resource_type: str
    provider_id: str
    region: str
    name: Optional[str]
    changed_fields: List[Dict[str, Any]]
    created_at: datetime


class ChangePage(Page):
    items: List[ChangeOut]


class ResourceDetailOut(ResourceOut):
    scope_key: str
    recent_changes: List[ChangeOut]


class ScanRunPage(Page):
    items: List[ScanRunSummary]


class ServiceCoverageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    service: str
    status: str
    complete: bool
    resources: int
    tasks: Dict[str, int]
    regions_affected: List[str]
    iam_actions: List[str]
    message: Optional[str]


class AccountStatsOut(BaseModel):
    account_id: uuid.UUID
    total_active: int
    total_deleted: int
    by_service: Dict[str, int]
    by_region: Dict[str, int]
    by_resource_type: Dict[str, int]
    by_project: Dict[str, int]
    by_status: Dict[str, int]
    last_scan: Optional[ScanRunSummary]
    last_scan_changes: Dict[str, int]
    last_scan_coverage: List[ServiceCoverageOut] = []


class DiffItemOut(BaseModel):
    resource_id: uuid.UUID
    category: str
    service: str
    resource_type: str
    provider_id: str
    region: str
    name: Optional[str]
    events: List[str]
    changed_fields: List[Dict[str, Any]]


class CompareOut(BaseModel):
    from_scan: ScanRunSummary
    to_scan: ScanRunSummary
    summary: Dict[str, Any]
    total_items: int
    items: List[DiffItemOut]


class AccountOverviewOut(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    projects: int
    regions: List[str]
    total_active: int
    total_deleted: int
    last_scan: Optional[ScanRunSummary]


class ClientOverviewOut(BaseModel):
    client: ClientOut
    accounts: List[AccountOverviewOut]


# ------------------------------------------------------------- Fase 4: programaciones
class ScheduleIn(BaseModel):
    """Campos permitidos al crear (sin asignación masiva de columnas internas)."""

    interval_minutes: int = Field(ge=15, le=10080, description="Frecuencia en minutos (15 min – 7 días)")
    name: Optional[str] = Field(default=None, max_length=200)
    services: Optional[List[str]] = None
    regions: Optional[List[str]] = None
    enabled: bool = True
    first_run_at: Optional[datetime] = None


class SchedulePatch(BaseModel):
    interval_minutes: Optional[int] = Field(default=None, ge=15, le=10080)
    name: Optional[str] = Field(default=None, max_length=200)
    services: Optional[List[str]] = None
    regions: Optional[List[str]] = None
    enabled: Optional[bool] = None


class ScheduleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    name: str
    enabled: bool
    interval_minutes: int
    services: Optional[List[str]]
    regions: Optional[List[str]]
    next_run_at: datetime
    last_run_id: Optional[uuid.UUID]
    last_triggered_at: Optional[datetime]
    last_status: Optional[str]
    last_error_safe: Optional[str]
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------- Fase 4: auditoría
class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    occurred_at: datetime
    actor_subject: str
    actor_kind: str
    client_id: Optional[uuid.UUID]
    account_id: Optional[uuid.UUID]
    action: str
    target_type: Optional[str]
    target_id: Optional[str]
    request_id: Optional[str]
    details: Dict[str, Any]


class AuditPage(Page):
    items: List[AuditEventOut]
