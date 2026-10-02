# coding: utf-8
"""Consultas de cuentas Huawei Cloud (siempre filtradas por cliente salvo mantenimiento)."""

from __future__ import annotations

import uuid
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import CloudAccount


def get_for_client(session: Session, client_id: uuid.UUID, account_id: uuid.UUID) -> Optional[CloudAccount]:
    return session.scalar(select(CloudAccount).where(CloudAccount.id == account_id,
                                                      CloudAccount.client_id == client_id))


def get_by_name(session: Session, client_id: uuid.UUID, name: str) -> Optional[CloudAccount]:
    return session.scalar(select(CloudAccount).where(CloudAccount.client_id == client_id,
                                                      CloudAccount.name == name))


def list_for_client(session: Session, client_id: uuid.UUID) -> List[CloudAccount]:
    return list(session.scalars(select(CloudAccount).where(CloudAccount.client_id == client_id)
                                .order_by(CloudAccount.name)))


def list_with_other_key_version(session: Session, key_version: int) -> List[CloudAccount]:
    """Mantenimiento (rotación de claves): recorre todas las cuentas."""
    return list(session.scalars(select(CloudAccount).where(CloudAccount.key_version != key_version)))
