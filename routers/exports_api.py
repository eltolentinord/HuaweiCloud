# coding: utf-8
"""Exportaciones del inventario persistido (Excel/CSV). Permiso: ``exports:read``.

- ``/exports/inventory``  inventario completo o filtrado (mismos filtros que /resources)
- ``/exports/summary``    resumen por servicio, región, tipo y estado + último escaneo
- ``/exports/compare``    diferencias entre dos escaneos (una hoja por categoría;
                          en CSV, una fila por recurso/campo con columna "Categoría")
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from core.authz import Permission
from db.session import get_db
from exports.excel import safe_filename_part
from exports.inventory_export import (
    MAX_EXPORT_ROWS,
    RESOURCE_COLUMNS,
    diff_rows,
    resource_rows,
    summary_rows,
    table,
    to_csv,
    to_xlsx,
)
from repositories import projects as projects_repo
from repositories import resources as resources_repo
from repositories import scans as scans_repo
from routers.security import requires
from scanning.history import compare_scans
from tenancy.accounts import get_account
from tenancy.errors import ValidationFailedError

router = APIRouter(prefix="/api/clients/{client_id}/accounts/{account_id}/exports", tags=["exports"],
                   dependencies=[Depends(requires(Permission.EXPORTS_READ))])

MEDIA = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         "csv": "text/csv; charset=utf-8"}
FormatParam = Query("xlsx", pattern="^(xlsx|csv)$")


def _download(buffer, fmt: str, account_name: str, kind: str) -> StreamingResponse:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{kind}_{safe_filename_part(account_name)}_{stamp}.{fmt}"
    return StreamingResponse(buffer, media_type=MEDIA[fmt],
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/inventory")
def export_inventory(client_id: uuid.UUID, account_id: uuid.UUID, format: str = FormatParam,
                     service: Optional[str] = None, region: Optional[str] = None,
                     resource_type: Optional[str] = None, project_id: Optional[uuid.UUID] = None,
                     status: Optional[str] = None, search: Optional[str] = Query(None, max_length=200),
                     include_deleted: bool = False, only_deleted: bool = False,
                     sort: Optional[str] = Query(None, max_length=200), db: Session = Depends(get_db)):
    account = get_account(db, client_id, account_id)
    try:
        rows, total = resources_repo.list_for_account(
            db, account.id, service=service, region=region, resource_type=resource_type, project_id=project_id,
            status=status, search=search, include_deleted=include_deleted, only_deleted=only_deleted,
            sort=sort, limit=MAX_EXPORT_ROWS, offset=0)
    except ValueError as exc:
        raise ValidationFailedError(str(exc)) from None
    if total > MAX_EXPORT_ROWS:
        raise ValidationFailedError(f"Demasiados recursos ({total}); aplica filtros (máx. {MAX_EXPORT_ROWS}).")
    projects = {p.id: p for p in projects_repo.list_for_account(db, account.id)}
    data = resource_rows(rows, projects)
    buffer = to_csv(RESOURCE_COLUMNS, data) if format == "csv" else to_xlsx([table("Recursos", data, RESOURCE_COLUMNS)])
    return _download(buffer, format, account.name, "inventario")


@router.get("/summary")
def export_summary(client_id: uuid.UUID, account_id: uuid.UUID, format: str = FormatParam,
                   db: Session = Depends(get_db)):
    account = get_account(db, client_id, account_id)
    last = scans_repo.list_for_account(db, account.id, limit=1)
    data = summary_rows(resources_repo.stats_for_account(db, account.id), last[0] if last else None)
    columns = ["Grupo", "Clave", "Valor"]
    buffer = to_csv(columns, data) if format == "csv" else to_xlsx([table("Resumen", data, columns)])
    return _download(buffer, format, account.name, "resumen")


@router.get("/compare")
def export_compare(client_id: uuid.UUID, account_id: uuid.UUID, from_scan: uuid.UUID, to_scan: uuid.UUID,
                   format: str = FormatParam, db: Session = Depends(get_db)):
    account = get_account(db, client_id, account_id)
    comparison = compare_scans(db, client_id=client_id, account_id=account.id,
                               from_scan=from_scan, to_scan=to_scan)
    sheets = diff_rows(comparison)
    if format == "csv":
        columns = ["Categoría", "Servicio", "Tipo", "ID proveedor", "Nombre", "Región", "Eventos",
                   "Campo", "Antes", "Después"]
        rows = [{"Categoría": category, **row} for category, items in sheets.items() for row in items]
        return _download(to_csv(columns, rows), format, account.name, "diferencias")
    summary = [{"Categoría": k, "Recursos": v} for k, v in comparison.summary()["counts"].items()]
    summary.insert(0, {"Categoría": f"Escaneo #{comparison.from_run.sequence} → #{comparison.to_run.sequence}",
                       "Recursos": ""})
    tables = [table("Resumen", summary, ["Categoría", "Recursos"])]
    tables += [table(name, items) for name, items in sheets.items()]
    return _download(to_xlsx(tables), format, account.name, "diferencias")
