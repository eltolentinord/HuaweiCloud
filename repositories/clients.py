# coding: utf-8
"""Consultas de clientes, usuarios y accesos."""

from __future__ import annotations

import uuid
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Client, User, UserClientRole


def get(session: Session, client_id: uuid.UUID) -> Optional[Client]:
    return session.get(Client, client_id)


def get_by_slug(session: Session, slug: str) -> Optional[Client]:
    return session.scalar(select(Client).where(Client.slug == slug))


def list_all(session: Session, *, status: Optional[str] = None) -> List[Client]:
    query = select(Client).order_by(Client.name)
    if status:
        query = query.where(Client.status == status)
    return list(session.scalars(query))


def get_user(session: Session, user_id: uuid.UUID) -> Optional[User]:
    return session.get(User, user_id)


def get_access(session: Session, user_id: uuid.UUID, client_id: uuid.UUID) -> Optional[UserClientRole]:
    return session.get(UserClientRole, (user_id, client_id))


def list_for_user(session: Session, user_id: uuid.UUID) -> List[Client]:
    query = (select(Client).join(UserClientRole, UserClientRole.client_id == Client.id)
             .join(User, User.id == UserClientRole.user_id)
             .where(UserClientRole.user_id == user_id, User.is_active.is_(True))
             .order_by(Client.name))
    return list(session.scalars(query))
