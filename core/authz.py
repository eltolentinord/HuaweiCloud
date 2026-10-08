# coding: utf-8
"""Autorización: roles, permisos y aislamiento por cliente (base para el SaaS multiusuario).

Modelo:
- ``Principal``: quién llama. ``platform_role="admin"`` = operador de la plataforma
  (acceso a todos los clientes). ``client_roles`` = rol por cliente para usuarios
  (tabla ``user_client_roles``).
- Roles por cliente (acumulativos): ``viewer`` ⊂ ``operator`` ⊂ ``admin``.
- Permisos de plataforma (``CLIENTS_MANAGE``): solo administradores de plataforma.

Respuesta ante falta de acceso:
- sin ningún rol en el cliente → 404 (no se revela que el cliente exista);
- con rol, pero insuficiente      → 403.

La autenticación (quién es) está separada: ``routers.security.get_principal``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, Iterable, Optional, Union

from sqlalchemy.orm import Session


class Role(str, Enum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


class Permission(str, Enum):
    CLIENTS_READ = "clients:read"
    CLIENTS_MANAGE = "clients:manage"            # plataforma: crear/modificar/borrar clientes
    ACCOUNTS_READ = "accounts:read"
    ACCOUNTS_MANAGE = "accounts:manage"          # crear/modificar/borrar cuentas
    CREDENTIALS_MANAGE = "credentials:manage"    # reemplazar AK/SK
    PROJECTS_MANAGE = "projects:manage"          # descubrir/añadir/habilitar proyectos
    SCANS_READ = "scans:read"
    SCANS_RUN = "scans:run"                      # lanza llamadas a Huawei Cloud
    INVENTORY_READ = "inventory:read"
    EXPORTS_READ = "exports:read"
    COSTS_READ = "costs:read"
    SCHEDULES_MANAGE = "schedules:manage"
    AUDIT_READ = "audit:read"                    # registro de auditoría del cliente
    DIAGNOSTICS_READ = "diagnostics:read"
    DIAGNOSTICS_MANAGE = "diagnostics:manage"
    DIAGNOSTICS_DELETE = "diagnostics:delete"    # solo admin


_VIEWER = frozenset({Permission.CLIENTS_READ, Permission.ACCOUNTS_READ, Permission.SCANS_READ,
                     Permission.INVENTORY_READ, Permission.EXPORTS_READ, Permission.COSTS_READ,
                     Permission.DIAGNOSTICS_READ})
_OPERATOR = _VIEWER | {Permission.SCANS_RUN, Permission.PROJECTS_MANAGE, Permission.SCHEDULES_MANAGE,
                        Permission.DIAGNOSTICS_MANAGE}
_ADMIN = _OPERATOR | {Permission.ACCOUNTS_MANAGE, Permission.CREDENTIALS_MANAGE, Permission.AUDIT_READ,
                      Permission.DIAGNOSTICS_DELETE}

ROLE_PERMISSIONS: Dict[Role, FrozenSet[Permission]] = {
    Role.VIEWER: _VIEWER, Role.OPERATOR: frozenset(_OPERATOR), Role.ADMIN: frozenset(_ADMIN),
}
PLATFORM_ONLY: FrozenSet[Permission] = frozenset({Permission.CLIENTS_MANAGE})


class AccessDenied(Exception):
    """``status_code`` 404 (cliente no visible) o 403 (permiso insuficiente)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class Principal:
    subject: str
    kind: str                                        # "service" | "local" | "user"
    platform_role: str = ""                          # "admin" = todos los clientes
    client_roles: Dict[str, str] = field(default_factory=dict)

    @property
    def is_platform_admin(self) -> bool:
        return self.platform_role == Role.ADMIN.value

    def role_for(self, client_id: Union[str, uuid.UUID, None]) -> Optional[Role]:
        if self.is_platform_admin:
            return Role.ADMIN
        if client_id is None:
            return None
        value = self.client_roles.get(str(client_id))
        return Role(value) if value in {r.value for r in Role} else None

    def visible_client_ids(self) -> Optional[FrozenSet[str]]:
        """``None`` = todos (plataforma); si no, los clientes con algún rol."""
        return None if self.is_platform_admin else frozenset(self.client_roles)


def can(principal: Principal, permission: Permission, client_id: Union[str, uuid.UUID, None] = None) -> bool:
    if permission in PLATFORM_ONLY:
        return principal.is_platform_admin
    role = principal.role_for(client_id)
    return role is not None and permission in ROLE_PERMISSIONS[role]


def ensure(principal: Principal, permission: Permission, client_id: Union[str, uuid.UUID, None] = None) -> None:
    if can(principal, permission, client_id):
        return
    if permission not in PLATFORM_ONLY and principal.role_for(client_id) is None:
        raise AccessDenied(404, "Cliente no encontrado.")
    raise AccessDenied(403, "Permiso insuficiente para esta operación.")


def principal_for_user(session: Session, user_id: uuid.UUID, *, platform_role: str = "") -> Optional[Principal]:
    """Construye el Principal de un usuario desde ``users``/``user_client_roles``.

    Punto de integración del futuro login: tras validar el token del proveedor de
    identidad se obtiene el ``users.id`` y se llama a esta función.
    """
    from db.models import User, UserClientRole
    from sqlalchemy import select

    user = session.get(User, user_id)
    if user is None or not user.is_active:
        return None
    roles: Iterable[UserClientRole] = session.scalars(select(UserClientRole).where(UserClientRole.user_id == user.id))
    return Principal(subject=str(user.id), kind="user", platform_role=platform_role,
                     client_roles={str(r.client_id): r.role for r in roles})
