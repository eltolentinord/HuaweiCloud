# coding: utf-8
"""API interna del motor de escaneo (montada junto a la API de administración).

- POST /api/clients/{client_id}/accounts/{account_id}/scans     inicia un escaneo (202)
- GET  /api/clients/{client_id}/accounts/{account_id}/scans     últimos escaneos
- GET  /api/scans/{scan_id}?client_id=...                       estado, progreso y tareas
- GET  /api/clients/{client_id}/accounts/{account_id}/resources inventario persistido

Sin login todavía: el cliente se indica explícitamente y TODAS las consultas se
filtran por él (aislamiento). El escaneo se ejecuta con ``BackgroundTasks`` de
FastAPI en este mismo proceso (sin Celery/ARQ/scheduler).
"""

from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.orm import Session, sessionmaker

from core.crypto import SecretCipher
from db.session import get_db, get_db_session_factory
from repositories import resources as resources_repo
from routers.deps import get_cipher
from repositories import scans as scans_repo
from scanning.engine import create_scan, execute_scan
from tenancy.accounts import get_account
from tenancy.errors import NotFoundError
from tenancy.schemas import ResourceOut, ResourcePage, ScanIn, ScanRunOut, ScanRunSummary

router = APIRouter(tags=["scans"])


def _run_in_background(factory: sessionmaker, cipher: SecretCipher, run_id: uuid.UUID) -> None:
    execute_scan(factory, cipher, run_id)


@router.post("/api/clients/{client_id}/accounts/{account_id}/scans", response_model=ScanRunSummary,
             status_code=202)
def start_scan(client_id: uuid.UUID, account_id: uuid.UUID, background: BackgroundTasks,
               body: Optional[ScanIn] = None, db: Session = Depends(get_db),
               factory: sessionmaker = Depends(get_db_session_factory),
               cipher: SecretCipher = Depends(get_cipher)):
    run = create_scan(db, client_id=client_id, account_id=account_id,
                      services=body.services if body else None, regions=body.regions if body else None,
                      project_ids=body.project_ids if body else None, trigger="api")
    db.commit()
    background.add_task(_run_in_background, factory, cipher, run.id)
    return ScanRunSummary.from_run(run)


@router.get("/api/clients/{client_id}/accounts/{account_id}/scans", response_model=List[ScanRunSummary])
def list_scans(client_id: uuid.UUID, account_id: uuid.UUID, limit: int = Query(20, ge=1, le=100),
               db: Session = Depends(get_db)):
    account = get_account(db, client_id, account_id)
    return [ScanRunSummary.from_run(r) for r in scans_repo.list_for_account(db, account.id, limit=limit)]


@router.get("/api/scans/{scan_id}", response_model=ScanRunOut)
def get_scan(scan_id: uuid.UUID, client_id: uuid.UUID = Query(..., description="Cliente propietario"),
             db: Session = Depends(get_db)):
    run = scans_repo.get_for_client(db, client_id, scan_id)
    if run is None:
        raise NotFoundError("Escaneo no encontrado.")
    return ScanRunOut.from_run(run, scans_repo.tasks(db, run.id))


@router.get("/api/clients/{client_id}/accounts/{account_id}/resources", response_model=ResourcePage)
def list_resources(client_id: uuid.UUID, account_id: uuid.UUID, service: Optional[str] = None,
                   region: Optional[str] = None, resource_type: Optional[str] = None,
                   project_id: Optional[uuid.UUID] = None, include_deleted: bool = False,
                   include_raw: bool = False, limit: int = Query(100, ge=1, le=1000),
                   offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    account = get_account(db, client_id, account_id)
    rows, total = resources_repo.list_for_account(
        db, account.id, service=service, region=region, resource_type=resource_type,
        project_id=project_id, include_deleted=include_deleted, limit=limit, offset=offset)
    return ResourcePage(total=total, limit=limit, offset=offset,
                        items=[ResourceOut.from_row(r, include_raw=include_raw) for r in rows])
