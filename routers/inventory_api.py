# coding: utf-8
"""API de inventario persistido: recursos, estadísticas, historial y comparación de escaneos.

Todas las rutas cuelgan de ``/api/clients/{client_id}`` y filtran SIEMPRE por cliente
(y cuenta): un ID de otro cliente devuelve 404. Listas paginadas con
``{items, total, limit, offset}``; ordenación por lista blanca.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.authz import Permission
from db.models import CHANGE_TYPES
from db.session import get_db
from repositories import changes as changes_repo
from repositories import projects as projects_repo
from repositories import resources as resources_repo
from repositories import scans as scans_repo
from routers.security import requires
from scanning.coverage import service_coverage
from scanning.history import CATEGORIES, compare_scans
from tenancy.accounts import get_account, list_accounts
from tenancy.clients import get_client
from tenancy.errors import NotFoundError, ValidationFailedError
from tenancy.schemas import (
    AccountOverviewOut,
    AccountStatsOut,
    ChangeOut,
    ChangePage,
    ClientOut,
    ClientOverviewOut,
    CompareOut,
    DiffItemOut,
    ResourceDetailOut,
    ResourceOut,
    ResourcePage,
    ScanRunSummary,
    ServiceCoverageOut,
)

# Todas las rutas de este router son de lectura de inventario del cliente.
router = APIRouter(prefix="/api/clients/{client_id}", tags=["inventory"],
                   dependencies=[Depends(requires(Permission.INVENTORY_READ))])


def _last_scan(db: Session, account_id: uuid.UUID) -> Optional[ScanRunSummary]:
    runs = scans_repo.list_for_account(db, account_id, limit=1)
    return ScanRunSummary.from_run(runs[0]) if runs else None


@router.get("/overview", response_model=ClientOverviewOut)
def client_overview(client_id: uuid.UUID, db: Session = Depends(get_db)):
    """Resumen para el dashboard: cuentas, proyectos, regiones, último escaneo e inventario."""
    client = get_client(db, client_id)
    accounts = []
    for account in list_accounts(db, client_id):
        projects = projects_repo.list_for_account(db, account.id)
        stats = resources_repo.stats_for_account(db, account.id)
        accounts.append(AccountOverviewOut(
            id=account.id, name=account.name, status=account.status, projects=len(projects),
            regions=sorted({p.region_id for p in projects}), total_active=stats["total_active"],
            total_deleted=stats["total_deleted"], last_scan=_last_scan(db, account.id)))
    return ClientOverviewOut(client=ClientOut.model_validate(client), accounts=accounts)


@router.get("/accounts/{account_id}/stats", response_model=AccountStatsOut)
def account_stats(client_id: uuid.UUID, account_id: uuid.UUID, db: Session = Depends(get_db)):
    account = get_account(db, client_id, account_id)
    last = _last_scan(db, account.id)
    return AccountStatsOut(account_id=account.id, **resources_repo.stats_for_account(db, account.id),
                           last_scan=last,
                           last_scan_changes=changes_repo.counts_for_run(db, last.id) if last else {},
                           last_scan_coverage=[ServiceCoverageOut.model_validate(c) for c in
                                               service_coverage(scans_repo.tasks(db, last.id))] if last else [])


@router.get("/accounts/{account_id}/resources", response_model=ResourcePage)
def list_resources(
    client_id: uuid.UUID, account_id: uuid.UUID,
    service: Optional[str] = None, region: Optional[str] = None, resource_type: Optional[str] = None,
    project_id: Optional[uuid.UUID] = None, status: Optional[str] = None,
    search: Optional[str] = Query(None, max_length=200, description="Nombre o ID del proveedor"),
    include_deleted: bool = False, only_deleted: bool = False, include_raw: bool = False,
    sort: Optional[str] = Query(None, max_length=200, description="p. ej. -last_seen,name"),
    limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    account = get_account(db, client_id, account_id)
    try:
        rows, total = resources_repo.list_for_account(
            db, account.id, service=service, region=region, resource_type=resource_type,
            project_id=project_id, status=status, search=search, include_deleted=include_deleted,
            only_deleted=only_deleted, sort=sort, limit=limit, offset=offset)
    except ValueError as exc:
        raise ValidationFailedError(str(exc)) from None
    return ResourcePage(total=total, limit=limit, offset=offset,
                        items=[ResourceOut.from_row(r, include_raw=include_raw) for r in rows])


def _resource(db: Session, client_id: uuid.UUID, account_id: uuid.UUID, resource_id: uuid.UUID):
    account = get_account(db, client_id, account_id)
    row = resources_repo.get_for_account(db, account.id, resource_id)
    if row is None:
        raise NotFoundError("Recurso no encontrado.")
    return row


@router.get("/accounts/{account_id}/resources/{resource_id}", response_model=ResourceDetailOut)
def resource_detail(client_id: uuid.UUID, account_id: uuid.UUID, resource_id: uuid.UUID,
                    db: Session = Depends(get_db)):
    row = _resource(db, client_id, account_id, resource_id)
    recent, _ = changes_repo.for_resource(db, row.id, limit=20)
    base = ResourceOut.from_row(row, include_raw=True).model_dump()
    return ResourceDetailOut(**base, scope_key=row.scope_key,
                             recent_changes=[ChangeOut.model_validate(c) for c in recent])


@router.get("/accounts/{account_id}/resources/{resource_id}/history", response_model=ChangePage)
def resource_history(client_id: uuid.UUID, account_id: uuid.UUID, resource_id: uuid.UUID,
                     limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                     db: Session = Depends(get_db)):
    row = _resource(db, client_id, account_id, resource_id)
    items, total = changes_repo.for_resource(db, row.id, limit=limit, offset=offset)
    return ChangePage(total=total, limit=limit, offset=offset,
                      items=[ChangeOut.model_validate(c) for c in items])


@router.get("/scans/{scan_id}/changes", response_model=ChangePage)
def scan_changes(client_id: uuid.UUID, scan_id: uuid.UUID,
                 change_type: Optional[str] = Query(None, description="created|updated|restored|deleted"),
                 service: Optional[str] = None, limit: int = Query(100, ge=1, le=1000),
                 offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    if change_type and change_type not in CHANGE_TYPES:
        raise ValidationFailedError(f"change_type debe ser uno de: {', '.join(CHANGE_TYPES)}")
    run = scans_repo.get_for_client(db, client_id, scan_id)
    if run is None:
        raise NotFoundError("Escaneo no encontrado.")
    items, total = changes_repo.for_run(db, run.id, change_type=change_type, service=service,
                                        limit=limit, offset=offset)
    return ChangePage(total=total, limit=limit, offset=offset,
                      items=[ChangeOut.model_validate(c) for c in items])


@router.get("/accounts/{account_id}/scans/compare", response_model=CompareOut)
def compare(client_id: uuid.UUID, account_id: uuid.UUID, from_scan: uuid.UUID, to_scan: uuid.UUID,
            category: Optional[str] = Query(None, description="added|removed|modified|transient"),
            limit: int = Query(200, ge=1, le=5000), offset: int = Query(0, ge=0),
            db: Session = Depends(get_db)):
    if category and category not in CATEGORIES:
        raise ValidationFailedError(f"category debe ser uno de: {', '.join(CATEGORIES)}")
    get_account(db, client_id, account_id)
    result = compare_scans(db, client_id=client_id, account_id=account_id, from_scan=from_scan, to_scan=to_scan)
    items = [i for i in result.items if not category or i.category == category]
    return CompareOut(from_scan=ScanRunSummary.from_run(result.from_run),
                      to_scan=ScanRunSummary.from_run(result.to_run), summary=result.summary(),
                      total_items=len(items),
                      items=[DiffItemOut(**vars(i)) for i in items[offset:offset + limit]])
