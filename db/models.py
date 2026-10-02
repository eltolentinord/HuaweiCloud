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
from datetime import datetime
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
    """Usuario de la plataforma (el login llegará en una fase posterior)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,
                                            server_default=true())

    client_roles: Mapped[List["UserClientRole"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"User(id={self.id})"


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
SCAN_TRIGGERS = ("manual", "api", "cli")
ACCOUNT_SCOPE_KEY = "account"  # recursos globales (OBS): uno por cuenta
ACTIVE_RUN_PREDICATE = "status IN ('pending', 'running')"

# JSONB en PostgreSQL; JSON genérico en SQLite (tests).
JSONType = JSON().with_variant(JSONB(), "postgresql")


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
