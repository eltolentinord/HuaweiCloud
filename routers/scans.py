# coding: utf-8
"""API interna del motor de escaneo (montada junto a la API de administración).

- POST /api/clients/{client_id}/accounts/{account_id}/scans     inicia un escaneo (202)
- GET  /api/clients/{client_id}/accounts/{account_id}/scans     últimos escaneos
- GET  /api/scans/{scan_id}?client_id=...                       estado, progreso y tareas
(El inventario persistido y el historial están en ``routers/inventory_api.py``.)

Sin login todavía: el cliente se indica explícitamente y TODAS las consultas se
filtran por él (aislamiento). El escaneo se ejecuta con ``BackgroundTasks`` de
FastAPI en este mismo proceso (sin Celery/ARQ/scheduler).
"""

from __future__ import annotations

import os
import uuid
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.orm import Session, sessionmaker

from core.crypto import SecretCipher
from db.session import get_db, get_db_session_factory
from core.authz import Permission, Principal
from routers.deps import get_cipher
from routers.security import get_principal, requires
from tenancy import audit
from repositories import scans as scans_repo
from scanning.engine import create_scan, execute_scan
from tenancy.accounts import get_account
from tenancy.errors import NotFoundError
from tenancy.schemas import ScanIn, ScanRunOut, ScanRunSummary

router = APIRouter(tags=["scans"])

ENV_SCAN_EXECUTOR = "INVENTORY_SCAN_EXECUTOR"


def scan_executor() -> str:
    """``inline`` (por defecto): BackgroundTasks del propio servidor web.
    ``worker``: la API solo encola; ejecuta el proceso ``manage.py worker`` (recomendado en producción).
    """
    value = os.environ.get(ENV_SCAN_EXECUTOR, "inline").strip().lower()
    return value if value in ("inline", "worker") else "inline"


def _run_in_background(factory: sessionmaker, cipher: SecretCipher, run_id: uuid.UUID) -> None:
    execute_scan(factory, cipher, run_id)


@router.post("/api/clients/{client_id}/accounts/{account_id}/scans", response_model=ScanRunSummary,
             status_code=202, dependencies=[Depends(requires(Permission.SCANS_RUN))])
def start_scan(client_id: uuid.UUID, account_id: uuid.UUID, background: BackgroundTasks,
               body: Optional[ScanIn] = None, db: Session = Depends(get_db),
               factory: sessionmaker = Depends(get_db_session_factory),
               cipher: SecretCipher = Depends(get_cipher), actor: Principal = Depends(get_principal)):
    run = create_scan(db, client_id=client_id, account_id=account_id,
                      services=body.services if body else None, regions=body.regions if body else None,
                      project_ids=body.project_ids if body else None, trigger="api")
    audit.record(db, actor, "scan.start", client_id=client_id, account_id=account_id, target=("scan", run.id),
                 details={"trigger": "api", "scan_sequence": run.sequence,
                          "services": run.stats.get("services"), "regions": run.stats.get("regions")})
    db.commit()
    if scan_executor() == "inline":
        background.add_task(_run_in_background, factory, cipher, run.id)
    # modo "worker": el escaneo queda en cola (pending) y lo ejecuta `manage.py worker`
    return ScanRunSummary.from_run(run)


@router.get("/api/clients/{client_id}/accounts/{account_id}/scans", response_model=List[ScanRunSummary],
            dependencies=[Depends(requires(Permission.SCANS_READ))])
def list_scans(client_id: uuid.UUID, account_id: uuid.UUID, limit: int = Query(20, ge=1, le=100),
               offset: int = Query(0, ge=0), status: Optional[str] = None, db: Session = Depends(get_db)):
    """Historial de escaneos (más reciente primero). Mantiene el formato de lista de la 3A."""
    account = get_account(db, client_id, account_id)
    runs = scans_repo.list_for_account(db, account.id, limit=limit, offset=offset, status=status)
    return [ScanRunSummary.from_run(r) for r in runs]


@router.get("/api/scans/{scan_id}", response_model=ScanRunOut,
            dependencies=[Depends(requires(Permission.SCANS_READ))])
def get_scan(scan_id: uuid.UUID, client_id: uuid.UUID = Query(..., description="Cliente propietario"),
             db: Session = Depends(get_db)):
    run = scans_repo.get_for_client(db, client_id, scan_id)
    if run is None:
        raise NotFoundError("Escaneo no encontrado.")
    return ScanRunOut.from_run(run, scans_repo.tasks(db, run.id))
