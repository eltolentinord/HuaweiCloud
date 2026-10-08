# coding: utf-8
"""Modelo de datos multi-cliente.

    Client ─┬─ CloudAccount ─┬─ Project ── Region
            │                ├─ ScanRun ── ScanTask (proyecto × servicio)
            │                └─ InventoryResource (tabla "resources")
            └─ UserClientRole ── User
    ServiceCatalog (15 servicios, sincronizado desde core.catalog + collectors)

Las credenciales de ``CloudAccount`` se guardan SOLO cifradas (``*_ciphertext``)
junto con la versión de la clave maestra usada (``key_version``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

CLIENT_STATUSES = ("active", "suspended", "archived")
ACCOUNT_STATUSES = ("pending", "active", "invalid", "disabled")
ROLES = ("viewer", "operator", "admin")
SERVICE_SCOPES = ("regional", "global")
DEFAULT_ENDPOINT_DOMAIN = "myhuaweicloud.com"


# JSONB en PostgreSQL; JSON genérico en SQLite (tests).
JSONType = JSON().with_variant(JSONB(), "postgresql")


def _in(column: str, values: tuple) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Client(TimestampMixin, Base):
    __tablename__ = "clients"
    __table_args__ = (CheckConstraint(_in("status", CLIENT_STATUSES), name="status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active",
                                        server_default="active")

    accounts: Mapped[List["CloudAccount"]] = relationship(
        back_populates="client", cascade="all, delete-orphan", passive_deletes=True
    )
    user_roles: Mapped[List["UserClientRole"]] = relationship(
        back_populates="client", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"Client(id={self.id}, slug={self.slug!r})"


class User(TimestampMixin, Base):
    """Usuario de la plataforma con login email+contraseña+OTP."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,
                                            server_default=true())
    password_hash: Mapped[Optional[str]] = mapped_column(Text)
    otp_code: Mapped[Optional[str]] = mapped_column(String(10))
    otp_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    client_roles: Mapped[List["UserClientRole"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    sessions: Mapped[List["UserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"User(id={self.id})"


class UserSession(Base):
    """Sesión activa de un usuario (token almacenado como SHA-256)."""

    __tablename__ = "user_sessions"
    __table_args__ = (Index("ix_user_sessions_token_hash", "token_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                          server_default="false")

    user: Mapped[User] = relationship(back_populates="sessions")


class UserClientRole(Base):
    """Acceso de un usuario a un cliente con un rol (un usuario → varios clientes)."""

    __tablename__ = "user_client_roles"
    __table_args__ = (CheckConstraint(_in("role", ROLES), name="role"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer",
                                      server_default="viewer")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="client_roles")
    client: Mapped[Client] = relationship(back_populates="user_roles")


class Region(TimestampMixin, Base):
    """Región Huawei Cloud. ``is_supported`` = visible en la interfaz (core.catalog)."""

    __tablename__ = "regions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    endpoint_domain: Mapped[str] = mapped_column(
        String(255), nullable=False, default=DEFAULT_ENDPOINT_DOMAIN,
        server_default=DEFAULT_ENDPOINT_DOMAIN,
    )
    is_supported: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,
                                               server_default=true())
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class CloudAccount(TimestampMixin, Base):
    """Cuenta Huawei Cloud de un cliente. Nunca contiene AK/SK en texto plano."""

    __tablename__ = "cloud_accounts"
    __table_args__ = (
        UniqueConstraint("client_id", "name"),
        CheckConstraint(_in("status", ACCOUNT_STATUSES), name="status"),
        CheckConstraint("key_version > 0", name="key_version_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    huawei_domain_id: Mapped[Optional[str]] = mapped_column(String(64))
    huawei_domain_name: Mapped[Optional[str]] = mapped_column(String(200))
    endpoint_domain: Mapped[str] = mapped_column(
        String(255), nullable=False, default=DEFAULT_ENDPOINT_DOMAIN,
        server_default=DEFAULT_ENDPOINT_DOMAIN,
    )
    iam_region_id: Mapped[Optional[str]] = mapped_column(String(64))
    ak_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sk_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending",
                                        server_default="pending")
    last_validated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_validation_error: Mapped[Optional[str]] = mapped_column(Text)
    # Último descubrimiento IAM: qué se encontró y qué no permitió la identidad IAM
    # (p. ej. "Permiso insuficiente" en Enterprise Projects). Sin secretos.
    last_discovery_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    discovery_report: Mapped[Optional[dict]] = mapped_column(JSONType)

    client: Mapped[Client] = relationship(back_populates="accounts")
    projects: Mapped[List["Project"]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # nunca incluye ciphertext ni secretos
        return f"CloudAccount(id={self.id}, name={self.name!r}, status={self.status!r})"


class Project(TimestampMixin, Base):
    """Project ID de Huawei Cloud (uno por región o subproyecto) de una cuenta."""

    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("account_id", "huawei_project_id"),
        Index("ix_projects_account_region", "account_id", "region_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cloud_accounts.id", ondelete="CASCADE"), nullable=False
    )
    huawei_project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    region_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("regions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[Optional[str]] = mapped_column(String(200))
    parent_huawei_project_id: Mapped[Optional[str]] = mapped_column(String(64))
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,
                                             server_default=true())
    huawei_enabled: Mapped[Optional[bool]] = mapped_column(Boolean)
    discovered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    account: Mapped[CloudAccount] = relationship(back_populates="projects")
    region: Mapped[Region] = relationship()


class ServiceCatalog(TimestampMixin, Base):
    """Servicios inventariables (uno por collector)."""

    __tablename__ = "service_catalog"
    __table_args__ = (CheckConstraint(_in("scope", SERVICE_SCOPES), name="scope"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default="regional",
                                       server_default="regional")
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,
                                             server_default=true())
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


# ============================================================================
# Fase 3A — motor de escaneo persistente
# ============================================================================

SCAN_RUN_STATUSES = ("pending", "running", "completed", "completed_with_warnings",
                     "completed_with_errors", "failed")
# denied = 401 de autorización IAM / 403; unavailable = API inexistente en la región (aviso).
SCAN_TASK_STATUSES = ("pending", "running", "succeeded", "partial", "denied", "unavailable",
                      "failed", "skipped")
SCAN_TRIGGERS = ("manual", "api", "cli", "schedule")
ACCOUNT_SCOPE_KEY = "account"  # recursos globales (OBS): uno por cuenta
ACTIVE_RUN_PREDICATE = "status IN ('pending', 'running')"


class ScanRun(Base):
    """Una ejecución de inventario de una cuenta (todos sus proyectos × servicios)."""

    __tablename__ = "scan_runs"
    __table_args__ = (
        CheckConstraint(_in("status", SCAN_RUN_STATUSES), name="status"),
        CheckConstraint(_in("trigger", SCAN_TRIGGERS), name="trigger"),
        Index("ix_scan_runs_account_created", "account_id", "created_at"),
        # Orden total y estable de los escaneos de una cuenta (no depende de la
        # resolución del reloj: dos escaneos en el mismo segundo siguen ordenados).
        UniqueConstraint("account_id", "sequence"),
        # Como mucho UN escaneo activo por cuenta, garantizado por la base de datos
        # (evita la carrera "comprobar y luego insertar" entre peticiones simultáneas).
        Index("uq_scan_runs_one_active_per_account", "account_id", unique=True,
              postgresql_where=text(ACTIVE_RUN_PREDICATE), sqlite_where=text(ACTIVE_RUN_PREDICATE)),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cloud_accounts.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending",
                                        server_default="pending")
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, default="manual",
                                         server_default="manual")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    total_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_resources: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_warnings: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    stats: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    error_message_safe: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    account: Mapped[CloudAccount] = relationship()
    tasks: Mapped[List["ScanTask"]] = relationship(
        back_populates="scan_run", cascade="all, delete-orphan", passive_deletes=True,
        order_by="ScanTask.sequence",
    )


class ScanTask(Base):
    """Una combinación proyecto × servicio (o cuenta × servicio global) de un ScanRun."""

    __tablename__ = "scan_tasks"
    __table_args__ = (
        CheckConstraint(_in("status", SCAN_TASK_STATUSES), name="status"),
        CheckConstraint(_in("scope", SERVICE_SCOPES), name="scope"),
        Index("ix_scan_tasks_run_sequence", "scan_run_id", "sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    scan_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("scan_runs.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # NULL en tareas globales (OBS); SET NULL conserva el historial si se borra el proyecto.
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    service: Mapped[str] = mapped_column(String(32), nullable=False)
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default="regional",
                                       server_default="regional")
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending",
                                        server_default="pending")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    resource_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    deleted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_kind: Mapped[Optional[str]] = mapped_column(String(32))
    iam_action: Mapped[Optional[str]] = mapped_column(String(128))
    http_status: Mapped[Optional[int]] = mapped_column(Integer)
    error_code: Mapped[Optional[str]] = mapped_column(String(128))
    request_id: Mapped[Optional[str]] = mapped_column(String(128))
    error_message_safe: Mapped[Optional[str]] = mapped_column(Text)
    notices: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)

    scan_run: Mapped[ScanRun] = relationship(back_populates="tasks")
    project: Mapped[Optional[Project]] = relationship()


class InventoryResource(TimestampMixin, Base):
    """Recurso Huawei normalizado y persistido (tabla ``resources``).

    Identidad: ``(account_id, resource_type, scope_key, provider_id)``.
    - ``provider_id`` NO se asume único entre cuentas ni entre proyectos.
    - ``scope_key`` = ``project:<uuid del proyecto>`` (regionales) o ``account``
      (globales como OBS): evita NULLs en la clave única (portable PG/SQLite).
    """

    __tablename__ = "resources"
    __table_args__ = (
        UniqueConstraint("account_id", "resource_type", "scope_key", "provider_id"),
        Index("ix_resources_account_service_scope", "account_id", "service", "scope_key"),
        Index("ix_resources_account_region", "account_id", "region"),
        Index("ix_resources_tags", "tags", postgresql_using="gin"),
        Index("ix_resources_attributes", "attributes", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cloud_accounts.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    scope_key: Mapped[str] = mapped_column(String(80), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    service: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(512))
    status: Mapped[Optional[str]] = mapped_column(String(64))
    enterprise_project_id: Mapped[Optional[str]] = mapped_column(String(64))
    provider_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    tags: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    attributes: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    raw: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    raw_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("scan_runs.id", ondelete="SET NULL"), index=True
    )

    def __repr__(self) -> str:
        return f"InventoryResource({self.resource_type} {self.provider_id})"


# ============================================================================
# Fase 4 — historial de cambios por recurso
# ============================================================================

CHANGE_TYPES = ("created", "updated", "restored", "deleted")


class ResourceChange(Base):
    """Evento de cambio de un recurso observado por un escaneo.

    Se escribe en el mismo upsert (``repositories.resources.apply_snapshot``):
    ``created`` / ``updated`` / ``restored`` / ``deleted``. ``changed_fields`` lista
    los campos normalizados que cambiaron (antes/después, ya redactados y truncados).
    La comparación entre escaneos usa ``resource_id`` (identidad estable), nunca el nombre.
    """

    __tablename__ = "resource_changes"
    __table_args__ = (
        CheckConstraint(_in("change_type", CHANGE_TYPES), name="change_type"),
        Index("ix_resource_changes_run_type", "scan_run_id", "change_type"),
        Index("ix_resource_changes_resource_created", "resource_id", "created_at"),
        Index("ix_resource_changes_account_created", "account_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cloud_accounts.id", ondelete="CASCADE"), nullable=False
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("resources.id", ondelete="CASCADE"), nullable=False
    )
    scan_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("scan_runs.id", ondelete="CASCADE"), nullable=False
    )
    change_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # Copia desnormalizada para listar cambios sin JOIN (y conservar el contexto).
    service: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    name: Mapped[Optional[str]] = mapped_column(String(512))
    changed_fields: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    raw_hash_before: Mapped[Optional[str]] = mapped_column(String(64))
    raw_hash_after: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ============================================================================
# Fase 4 — programación de escaneos (scheduler)
# ============================================================================

MIN_SCHEDULE_MINUTES = 15
MAX_SCHEDULE_MINUTES = 7 * 24 * 60


class ScanSchedule(TimestampMixin, Base):
    """Escaneo periódico de una cuenta, ejecutado por el worker (``manage.py worker``).

    El worker reclama los vencidos con ``FOR UPDATE SKIP LOCKED`` (varios workers no
    duplican ejecuciones) y avanza ``next_run_at`` ANTES de lanzar el escaneo.
    """

    __tablename__ = "scan_schedules"
    __table_args__ = (
        CheckConstraint(f"interval_minutes BETWEEN {MIN_SCHEDULE_MINUTES} AND {MAX_SCHEDULE_MINUTES}",
                        name="interval_range"),
        Index("ix_scan_schedules_due", "enabled", "next_run_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cloud_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="Escaneo programado")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    services: Mapped[Optional[list]] = mapped_column(JSONType)   # None = todos los habilitados
    regions: Mapped[Optional[list]] = mapped_column(JSONType)    # None = todas las de la cuenta
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("scan_runs.id", ondelete="SET NULL")
    )
    last_triggered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[Optional[str]] = mapped_column(String(32))   # queued | skipped_active | error
    last_error_safe: Mapped[Optional[str]] = mapped_column(Text)


# ============================================================================
# Fase 4 — auditoría de acciones administrativas
# ============================================================================


class AuditEvent(Base):
    """Quién hizo qué y cuándo (alta de cuentas, credenciales, escaneos, programaciones...).

    ``client_id``/``account_id`` NO son claves foráneas a propósito: el registro debe
    sobrevivir al borrado del cliente o la cuenta. ``details`` solo admite campos
    explícitos y nunca contiene AK/SK ni otros secretos.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_client_occurred", "client_id", "occurred_at"),
        Index("ix_audit_events_action", "action"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    actor_subject: Mapped[str] = mapped_column(String(200), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    client_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[Optional[str]] = mapped_column(String(32))
    target_id: Mapped[Optional[str]] = mapped_column(String(64))
    request_id: Mapped[Optional[str]] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)


# ---------------------------------------------------------------- Comparador de costos por región
# Migración 0011 (calculadora): traffic = ancho de banda por tráfico (GB), rds/rds_storage, obs_storage.
PRICE_PRODUCTS = ("ecs", "evs", "ip", "bandwidth", "traffic", "rds", "rds_storage", "obs_storage")
BILLING_MODES = ("on_demand", "monthly", "yearly")
PRICE_PERIODS = ("hour", "month", "year", "gb")
PRICE_SOURCES = ("huawei_bss",)


class PriceCatalogEntry(Base):
    """Precio OFICIAL de lista consultado a Huawei Cloud (nunca un valor inventado).

    Es información pública de Huawei (``official_website_amount``), no datos del
    cliente: el catálogo es compartido y no guarda descuentos ni importes facturados.
    ``size`` es la cantidad exacta cotizada (GB, Mbps) o 0 si el producto no es lineal;
    ``amount`` es el precio de esa configuración para un ``period`` (hora o mes).
    """

    __tablename__ = "price_catalog_entries"
    __table_args__ = (
        UniqueConstraint("source", "region", "product", "spec", "billing_mode", "size", name="uq_price_catalog_key"),
        CheckConstraint(_in("product", PRICE_PRODUCTS), name="product"),
        CheckConstraint(_in("billing_mode", BILLING_MODES), name="billing_mode"),
        CheckConstraint(_in("period", PRICE_PERIODS), name="period"),
        CheckConstraint(_in("source", PRICE_SOURCES), name="source"),
        CheckConstraint("amount >= 0", name="amount_non_negative"),
        Index("ix_price_catalog_lookup", "region", "product", "spec", "billing_mode"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_detail: Mapped[str] = mapped_column(String(200), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    product: Mapped[str] = mapped_column(String(16), nullable=False)
    spec: Mapped[str] = mapped_column(String(100), nullable=False)
    billing_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    amount: Mapped[object] = mapped_column(Numeric(20, 8), nullable=False)
    period: Mapped[str] = mapped_column(String(8), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FlavorCatalogEntry(Base):
    """Flavor ECS disponible en una región según ``ListFlavors`` (información pública)."""

    __tablename__ = "flavor_catalog"
    __table_args__ = (UniqueConstraint("region", "flavor_id"), Index("ix_flavor_catalog_region", "region"))

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    flavor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    vcpus: Mapped[int] = mapped_column(Integer, nullable=False)
    ram_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    performance_type: Mapped[Optional[str]] = mapped_column(String(64))
    generation: Mapped[Optional[str]] = mapped_column(String(32))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Detalle oficial (migración 0010; NULL en filas consultadas antes de ella).
    status: Mapped[Optional[str]] = mapped_column(String(16))          # cond:operation:status
    az_status: Mapped[Optional[dict]] = mapped_column(JSONType)       # cond:operation:az -> {az: estado}
    architecture: Mapped[Optional[str]] = mapped_column(String(16))    # ecs:instance_architecture (arm64)
    cpu_name: Mapped[Optional[str]] = mapped_column(String(128))       # info:cpu:name
    gpu_name: Mapped[Optional[str]] = mapped_column(String(128))       # info:gpu:name
    max_bandwidth_gbps: Mapped[Optional[object]] = mapped_column(Numeric(10, 2))  # quota:max_rate (Mbit/s -> Gbit/s)
    max_pps: Mapped[Optional[int]] = mapped_column(Integer)            # quota:max_pps


class VolumeTypeCatalogEntry(Base):
    """Tipo de disco EVS de una región según ``CinderListVolumeTypes`` (información pública)."""

    __tablename__ = "volume_type_catalog"
    __table_args__ = (UniqueConstraint("region", "name"), Index("ix_volume_type_catalog_region", "region"))

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    availability_zones: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    sold_out_zones: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)



class RdsFlavorCatalogEntry(Base):
    """Flavor de RDS de una región y motor según RDS ``ListFlavors`` (información pública)."""

    __tablename__ = "rds_flavor_catalog"
    __table_args__ = (UniqueConstraint("region", "engine", "engine_version", "spec_code"),
                      Index("ix_rds_flavor_catalog_region_engine", "region", "engine"))

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    spec_code: Mapped[str] = mapped_column(String(100), nullable=False)
    vcpus: Mapped[int] = mapped_column(Integer, nullable=False)
    ram_gb: Mapped[object] = mapped_column(Numeric(10, 2), nullable=False)
    instance_mode: Mapped[str] = mapped_column(String(16), nullable=False)   # single | ha | replica
    az_status: Mapped[Optional[dict]] = mapped_column(JSONType)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


BSS_CODE_KINDS = ("service", "resource", "usage")


class BssCodeCatalogEntry(Base):
    """Código de BSS confirmado con ``ListServiceTypes``/``ListResourceTypes``/``ListUsageTypes``."""

    __tablename__ = "bss_code_catalog"
    __table_args__ = (UniqueConstraint("kind", "code"), CheckConstraint(_in("kind", BSS_CODE_KINDS), name="kind"))

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(200))
    parent_code: Mapped[Optional[str]] = mapped_column(String(128))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

# ---------------------------------------------------------------- Enterprise Projects (IAM Discovery)
class EnterpriseProject(TimestampMixin, Base):
    """Enterprise Project de Huawei Cloud visible para la identidad IAM de una cuenta.

    Pertenece a HUAWEI CLOUD (no es un tenant de la plataforma). Se sincroniza con
    EPS ``ListEnterpriseProject``; los recursos lo referencian por
    ``resources.enterprise_project_id`` (= ``huawei_ep_id``). No se borra si deja de
    aparecer: se marca ``present = false``.
    """

    __tablename__ = "enterprise_projects"
    __table_args__ = (UniqueConstraint("account_id", "huawei_ep_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cloud_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    huawei_ep_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[Optional[int]] = mapped_column(Integer)          # 1 habilitado, 2 deshabilitado (EPS)
    ep_type: Mapped[Optional[str]] = mapped_column(String(16))      # prod | poc (EPS)
    present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    discovered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- Auditoría de servidores (SSH)
SERVER_AUDIT_STATUSES = ("queued", "running", "succeeded", "failed")


class Server(TimestampMixin, Base):
    """Servidor Linux de un cliente que se audita por SSH con ``server_audit/scripts/server-audit.sh``.

    La contraseña (SSH y sudo) se guarda CIFRADA (``SecretCipher``) y nunca se devuelve.
    ``host_fingerprint`` = huella SHA256 de la clave del servidor (primera conexión, TOFU)."""

    __tablename__ = "servers"
    __table_args__ = (UniqueConstraint("client_id", "name"),
                      CheckConstraint("port BETWEEN 1 AND 65535", name="port_range"),
                      CheckConstraint("key_version > 0", name="key_version_positive"))

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=22, server_default="22")
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    host_fingerprint: Mapped[Optional[str]] = mapped_column(String(128))
    description: Mapped[Optional[str]] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())


class ServerAuditRun(Base):
    """Una ejecución de ``server-audit.sh`` en un servidor (``batch_id`` agrupa «Auditar todos»)."""

    __tablename__ = "server_audit_runs"
    __table_args__ = (CheckConstraint(_in("status", SERVER_AUDIT_STATUSES), name="status"),
                      Index("ix_server_audit_runs_client_batch", "client_id", "batch_id"),
                      Index("ix_server_audit_runs_server_created", "server_id", "created_at"))

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    server_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("servers.id", ondelete="SET NULL"))
    server_name: Mapped[str] = mapped_column(String(100), nullable=False)   # se conserva si se borra el servidor
    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    # Marca con microsegundos desde Python: el orden "última auditoría" no depende de la precisión de la base.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False,
                                                 default=lambda: datetime.now(timezone.utc))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    script_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    hostname: Mapped[Optional[str]] = mapped_column(String(255))
    os: Mapped[Optional[str]] = mapped_column(String(255))
    overall_score: Mapped[Optional[int]] = mapped_column(Integer)
    hardening_pct: Mapped[Optional[int]] = mapped_column(Integer)
    updates_pct: Mapped[Optional[int]] = mapped_column(Integer)
    result_json: Mapped[Optional[dict]] = mapped_column(JSONType)
    report_html: Mapped[Optional[str]] = mapped_column(Text)
    error_kind: Mapped[Optional[str]] = mapped_column(String(32))
    error_message: Mapped[Optional[str]] = mapped_column(String(1000))
    requested_by: Mapped[Optional[str]] = mapped_column(String(200))


# ============================================================================
# Módulo Cloud Eye Auto-Diagnóstico
# ============================================================================

DIAGNOSTIC_INCIDENT_STATUSES = (
    "alert_received", "validating", "pending_diagnosis", "connecting",
    "analyzing", "generating_report", "report_available", "failed",
    "no_server", "recovered",
)
IN_PROGRESS_STATUSES: frozenset = frozenset({
    "alert_received", "validating", "pending_diagnosis",
    "connecting", "analyzing", "generating_report",
})


class CesAlarmEvent(Base):
    """Evento de alarma CES recibido desde SMN (webhook). Nunca se borra."""

    __tablename__ = "ces_alarm_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_ces_alarm_events_idempotency_key"),
        Index("ix_ces_alarm_events_account_fired", "account_id", "fired_at"),
        Index("ix_ces_alarm_events_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("cloud_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    alarm_id: Mapped[str] = mapped_column(String(255), nullable=False)
    alarm_name: Mapped[Optional[str]] = mapped_column(String(500))
    namespace: Mapped[Optional[str]] = mapped_column(String(128))
    metric_name: Mapped[Optional[str]] = mapped_column(String(128))
    threshold: Mapped[Optional[object]] = mapped_column(Numeric(20, 8))
    observed_value: Mapped[Optional[object]] = mapped_column(Numeric(20, 8))
    alarm_level: Mapped[Optional[int]] = mapped_column(Integer)
    resource_id: Mapped[Optional[str]] = mapped_column(String(255))
    alarm_status: Mapped[Optional[str]] = mapped_column(String(64))
    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_event_safe: Mapped[Optional[dict]] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="received", server_default="received"
    )
    # diagnostic_id apunta al incidente asociado; se pone NULL cuando el incidente se elimina
    diagnostic_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, nullable=True)


class DiagnosticIncident(TimestampMixin, Base):
    """Incidente de diagnóstico generado a partir de un evento de alarma CES."""

    __tablename__ = "diagnostic_incidents"
    __table_args__ = (
        CheckConstraint(_in("status", DIAGNOSTIC_INCIDENT_STATUSES), name="status"),
        Index("ix_diagnostic_incidents_client_created", "client_id", "created_at"),
        Index("ix_diagnostic_incidents_account_created", "account_id", "created_at"),
        Index("ix_diagnostic_incidents_status", "status"),
        Index("ix_diagnostic_incidents_server", "server_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    client_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("cloud_accounts.id", ondelete="SET NULL"), nullable=True
    )
    event_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("ces_alarm_events.id", ondelete="SET NULL"), nullable=True
    )
    server_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("servers.id", ondelete="SET NULL"), nullable=True
    )
    ecs_name: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", server_default=""
    )
    ecs_instance_id: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    ecs_ip: Mapped[Optional[str]] = mapped_column(String(128))
    region: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    project_id_hw: Mapped[Optional[str]] = mapped_column(String(64))
    enterprise_project_id: Mapped[Optional[str]] = mapped_column(String(64))
    alarm_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="unknown", server_default="unknown"
    )
    metric_name: Mapped[Optional[str]] = mapped_column(String(128))
    threshold: Mapped[Optional[object]] = mapped_column(Numeric(20, 8))
    observed_value: Mapped[Optional[object]] = mapped_column(Numeric(20, 8))
    severity: Mapped[Optional[int]] = mapped_column(Integer)
    alarm_fired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="alert_received", server_default="alert_received"
    )
    possible_cause: Mapped[Optional[str]] = mapped_column(Text)
    confidence: Mapped[Optional[str]] = mapped_column(String(16))
    pdf_data: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    pdf_name: Mapped[Optional[str]] = mapped_column(String(255))
    report_json: Mapped[Optional[dict]] = mapped_column(JSONType)
    reviewed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    error_kind: Mapped[Optional[str]] = mapped_column(String(32))
    error_message_safe: Mapped[Optional[str]] = mapped_column(Text)

    commands: Mapped[List["DiagnosticCommand"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan", passive_deletes=True
    )
    evidence: Mapped[List["DiagnosticEvidence"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan", passive_deletes=True
    )


class DiagnosticCommand(Base):
    """Comando SSH ejecutado durante un diagnóstico (solo lectura; shell=False)."""

    __tablename__ = "diagnostic_commands"
    __table_args__ = (
        Index("ix_diagnostic_commands_incident_seq", "incident_id", "sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("diagnostic_incidents.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    command: Mapped[str] = mapped_column(String(255), nullable=False)
    args: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    exit_code: Mapped[Optional[int]] = mapped_column(Integer)
    stdout_safe: Mapped[Optional[str]] = mapped_column(Text)
    stderr_safe: Mapped[Optional[str]] = mapped_column(Text)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    incident: Mapped["DiagnosticIncident"] = relationship(back_populates="commands")


class DiagnosticEvidence(Base):
    """Evidencia estructurada extraída de un diagnóstico."""

    __tablename__ = "diagnostic_evidence"
    __table_args__ = (
        Index("ix_diagnostic_evidence_incident", "incident_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("diagnostic_incidents.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    summary_safe: Mapped[Optional[str]] = mapped_column(Text)
    data_json: Mapped[Optional[dict]] = mapped_column(JSONType)
    relevance_score: Mapped[Optional[int]] = mapped_column(Integer)

    incident: Mapped["DiagnosticIncident"] = relationship(back_populates="evidence")


class DiagnosticAuditLog(Base):
    """Registro permanente de acciones sobre diagnósticos (sobrevive al borrado del incidente)."""

    __tablename__ = "diagnostic_audit_logs"
    __table_args__ = (
        Index("ix_diagnostic_audit_logs_account", "account_id"),
        Index("ix_diagnostic_audit_logs_performed_at", "performed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Sin FK a diagnostic_incidents: sobrevive al borrado del incidente
    diagnostic_id_original: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    incident_number: Mapped[Optional[str]] = mapped_column(String(64))
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)
    server_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)
    server_name: Mapped[Optional[str]] = mapped_column(String(255))
    ecs_name: Mapped[Optional[str]] = mapped_column(String(512))
    action: Mapped[str] = mapped_column(
        String(64), nullable=False, default="diagnostic_deleted"
    )
    performed_by: Mapped[str] = mapped_column(String(200), nullable=False)
    performed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    reason: Mapped[Optional[str]] = mapped_column(Text)
    metadata_safe: Mapped[Optional[dict]] = mapped_column(JSONType)
