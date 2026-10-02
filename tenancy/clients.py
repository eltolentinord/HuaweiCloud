# coding: utf-8
"""CRUD de clientes y acceso de usuarios a clientes."""

from __future__ import annotations

import re
import unicodedata
import uuid
from typing import List, Optional

from sqlalchemy.orm import Session

from db.models import CLIENT_STATUSES, ROLES, Client, UserClientRole
from repositories import clients as repo
from tenancy.errors import ConflictError, NotFoundError, ValidationFailedError

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def slugify(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")[:80]


def _validate(name: Optional[str], slug: Optional[str], status: Optional[str]) -> None:
    if name is not None and not name.strip():
        raise ValidationFailedError("El nombre del cliente es obligatorio.")
    if slug is not None and not SLUG_PATTERN.match(slug):
        raise ValidationFailedError("El slug solo admite minúsculas, números y guiones.")
    if status is not None and status not in CLIENT_STATUSES:
        raise ValidationFailedError(f"Estado inválido; usa uno de {', '.join(CLIENT_STATUSES)}.")


def _ensure_unique_slug(session: Session, slug: str, exclude: Optional[uuid.UUID] = None) -> None:
    existing = repo.get_by_slug(session, slug)
    if existing is not None and existing.id != exclude:
        raise ConflictError(f"Ya existe un cliente con el slug '{slug}'.")


def create_client(session: Session, *, name: str, slug: Optional[str] = None,
                  status: str = "active") -> Client:
    slug = slug or slugify(name)
    _validate(name, slug, status)
    _ensure_unique_slug(session, slug)
    client = Client(name=name.strip(), slug=slug, status=status)
    session.add(client)
    session.flush()
    return client


def get_client(session: Session, client_id: uuid.UUID) -> Client:
    client = repo.get(session, client_id)
    if client is None:
        raise NotFoundError("Cliente no encontrado.")
    return client


def get_client_by_slug(session: Session, slug: str) -> Client:
    client = repo.get_by_slug(session, slug)
    if client is None:
        raise NotFoundError("Cliente no encontrado.")
    return client


def list_clients(session: Session, *, status: Optional[str] = None) -> List[Client]:
    return repo.list_all(session, status=status)


def update_client(session: Session, client_id: uuid.UUID, *, name: Optional[str] = None,
                  slug: Optional[str] = None, status: Optional[str] = None) -> Client:
    client = get_client(session, client_id)
    _validate(name, slug, status)
    if slug is not None and slug != client.slug:
        _ensure_unique_slug(session, slug, exclude=client.id)
        client.slug = slug
    if name is not None:
        client.name = name.strip()
    if status is not None:
        client.status = status
    session.flush()
    return client


def delete_client(session: Session, client_id: uuid.UUID) -> None:
    """Borra el cliente y, en cascada, sus cuentas (y sus secretos cifrados) y proyectos."""
    session.delete(get_client(session, client_id))
    session.flush()


# ------------------------------------------------------- acceso de usuarios
def grant_access(session: Session, *, user_id: uuid.UUID, client_id: uuid.UUID,
                 role: str = "viewer") -> UserClientRole:
    if role not in ROLES:
        raise ValidationFailedError(f"Rol inválido; usa uno de {', '.join(ROLES)}.")
    if repo.get_user(session, user_id) is None:
        raise NotFoundError("Usuario no encontrado.")
    get_client(session, client_id)
    access = repo.get_access(session, user_id, client_id)
    if access is None:
        access = UserClientRole(user_id=user_id, client_id=client_id, role=role)
        session.add(access)
    else:
        access.role = role
    session.flush()
    return access


def clients_for_user(session: Session, user_id: uuid.UUID) -> List[Client]:
    """Clientes a los que un usuario activo tiene acceso (base del futuro RBAC)."""
    return repo.list_for_user(session, user_id)
