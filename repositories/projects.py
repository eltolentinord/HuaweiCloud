# coding: utf-8
"""Consultas de proyectos Huawei de una cuenta."""

from __future__ import annotations

import uuid
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import CloudAccount, Project


def list_for_account(session: Session, account_id: uuid.UUID, *, enabled_only: bool = False) -> List[Project]:
    query = select(Project).where(Project.account_id == account_id)
    if enabled_only:
        query = query.where(Project.is_enabled.is_(True))
    return list(session.scalars(query.order_by(Project.region_id, Project.name, Project.huawei_project_id)))


def by_huawei_id(session: Session, account_id: uuid.UUID) -> Dict[str, Project]:
    return {p.huawei_project_id: p for p in session.scalars(
        select(Project).where(Project.account_id == account_id))}


def get_by_huawei_id(session: Session, account_id: uuid.UUID, huawei_project_id: str) -> Optional[Project]:
    return session.scalar(select(Project).where(Project.account_id == account_id,
                                                 Project.huawei_project_id == huawei_project_id))


def get_for_client(session: Session, client_id: uuid.UUID, account_id: uuid.UUID,
                   project_id: uuid.UUID) -> Optional[Project]:
    query = (select(Project).join(CloudAccount, CloudAccount.id == Project.account_id)
             .where(Project.id == project_id, Project.account_id == account_id,
                    CloudAccount.client_id == client_id))
    return session.scalar(query)
