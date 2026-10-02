# coding: utf-8
"""Registro de auditoría del cliente (permiso ``audit:read``: administradores del cliente)."""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.authz import Permission
from db.session import get_db
from routers.security import requires
from tenancy import audit
from tenancy.clients import get_client
from tenancy.schemas import AuditEventOut, AuditPage

router = APIRouter(prefix="/api/clients/{client_id}/audit", tags=["audit"],
                   dependencies=[Depends(requires(Permission.AUDIT_READ))])


@router.get("", response_model=AuditPage)
def list_audit(client_id: uuid.UUID, action: Optional[str] = Query(None, max_length=64),
               limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
               db: Session = Depends(get_db)):
    get_client(db, client_id)
    rows, total = audit.list_for_client(db, client_id, action=action, limit=limit, offset=offset)
    return AuditPage(total=total, limit=limit, offset=offset, items=[AuditEventOut.model_validate(r) for r in rows])
