# coding: utf-8
"""Costos por cuenta (permiso ``costs:read``). Sin AK/SK desde el navegador.

- ``/costs/estimate``  costo ESTIMADO por recurso con el proveedor de precios configurado
- ``/costs/actual``    costo REAL facturado por recurso en un mes (BSS; llama a Huawei)
- ``/costs/compare``   comparación de dos meses facturados (servicio/región/proyecto)

``format=xlsx|csv`` devuelve una exportación; por defecto JSON.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from core.authz import Permission
from core.clients import ClientFactory
from core.crypto import SecretCipher
from core.errors import classify_exception
from costs import billing, pricing
from costs.report import CostReport
from db.session import get_db
from exports.inventory_export import table, to_csv, to_xlsx
from repositories import projects as projects_repo
from repositories import resources as resources_repo
from routers.deps import get_cipher
from routers.exports_api import _download
from routers.security import requires
from tenancy.accounts import decrypt_credentials, get_account
from tenancy.errors import ValidationFailedError

router = APIRouter(prefix="/api/clients/{client_id}/accounts/{account_id}/costs", tags=["costs"],
                   dependencies=[Depends(requires(Permission.COSTS_READ))])

# Fábricas: cada parámetro necesita su propio objeto Query (compartirlo enlaza mal los nombres).
def MonthParam():
    return Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="YYYY-MM")


def FormatParam():
    return Query("json", pattern="^(json|xlsx|csv)$")
LINE_COLUMNS = ["Servicio", "Región", "Proyecto", "ID proveedor", "Nombre", "Importe", "Moneda", "Precio lista", "Base"]
MAX_LINES_JSON = 1000


def _context(db: Session, client_id: uuid.UUID, account_id: uuid.UUID):
    account = get_account(db, client_id, account_id)
    resources, _ = resources_repo.list_for_account(db, account.id, limit=100_000)
    projects = {p.id: p.huawei_project_id for p in projects_repo.list_for_account(db, account.id)}
    return account, resources, projects


def _line_rows(report: CostReport) -> List[Dict[str, Any]]:
    return [{"Servicio": l.service, "Región": l.region, "Proyecto": l.project or "", "ID proveedor": l.provider_id,
             "Nombre": l.name or "", "Importe": str(l.amount), "Moneda": l.currency,
             "Precio lista": str(l.official_amount) if l.official_amount is not None else "", "Base": l.basis}
            for l in report.lines + report.unmatched]


def _payload(report: CostReport) -> Dict[str, Any]:
    lines = _line_rows(report)
    return {"kind": report.kind, "source": report.source, "period": report.period, "message": report.message,
            "coverage": report.coverage(), "totals": report.totals(), "lines_total": len(lines),
            "lines": lines[:MAX_LINES_JSON], "unpriced": report.unpriced[:MAX_LINES_JSON]}


def _respond(report: CostReport, fmt: str, account_name: str):
    if fmt == "json":
        return _payload(report)
    rows = _line_rows(report)
    if fmt == "csv":
        return _download(to_csv(LINE_COLUMNS, rows), fmt, account_name, f"costos_{report.kind}")
    summary = [{"Moneda": currency, "Grupo": group, "Clave": key, "Importe": value}
               for currency, data in report.totals().items()
               for group, values in (("Servicio", data["by_service"]), ("Región", data["by_region"]),
                                     ("Proyecto", data["by_project"]))
               for key, value in values.items()]
    summary.insert(0, {"Moneda": "", "Grupo": "Fuente", "Clave": report.source, "Importe": report.period or ""})
    tables = [table("Resumen", summary, ["Moneda", "Grupo", "Clave", "Importe"]), table("Recursos", rows, LINE_COLUMNS)]
    if report.unpriced:
        tables.append(table("Sin precio", report.unpriced))
    return _download(to_xlsx(tables), fmt, account_name, f"costos_{report.kind}")


@router.get("/estimate")
def estimate(client_id: uuid.UUID, account_id: uuid.UUID, format: str = FormatParam(), db: Session = Depends(get_db)):
    account, resources, projects = _context(db, client_id, account_id)
    try:
        provider = pricing.provider_from_env()
    except pricing.PriceTableError as exc:
        raise ValidationFailedError(f"Tabla de precios inválida: {exc}") from None
    return _respond(pricing.estimate(resources, provider, projects), format, account.name)


def _actual(db: Session, cipher: SecretCipher, client_id: uuid.UUID, account_id: uuid.UUID, month: str):
    account, resources, projects = _context(db, client_id, account_id)
    credentials = decrypt_credentials(account, cipher)
    clients = ClientFactory(credentials, endpoint_domain=account.endpoint_domain)
    try:
        bills = billing.fetch_resource_bills(clients, month)
    except Exception as exc:  # clasificado y redactado: nunca AK/SK ni detalles internos
        error = classify_exception(exc, service="bss", secrets=credentials.secrets)
        raise HTTPException(status_code=502, detail={
            "mensaje": error.message, "categoria": error.kind, "http_status": error.http_status,
            "error_code": error.error_code, "request_id": error.request_id, "accion_iam": error.iam_action})
    return account, billing.build_actual_report(month, bills, resources, projects)


@router.get("/actual")
def actual(client_id: uuid.UUID, account_id: uuid.UUID, month: str = MonthParam(), format: str = FormatParam(),
           db: Session = Depends(get_db), cipher: SecretCipher = Depends(get_cipher)):
    account, report = _actual(db, cipher, client_id, account_id, month)
    return _respond(report, format, account.name)


@router.get("/compare")
def compare(client_id: uuid.UUID, account_id: uuid.UUID, month_a: str = MonthParam(), month_b: str = MonthParam(),
            db: Session = Depends(get_db), cipher: SecretCipher = Depends(get_cipher)) -> Dict[str, Any]:
    if month_a == month_b:
        raise ValidationFailedError("Indica dos meses distintos.")
    _, report_a = _actual(db, cipher, client_id, account_id, month_a)
    _, report_b = _actual(db, cipher, client_id, account_id, month_b)
    by_project: Dict[str, Dict[str, str]] = {}
    for label, report in (("mes_a", report_a), ("mes_b", report_b)):
        for currency, data in report.totals().items():
            for project, value in data["by_project"].items():
                by_project.setdefault(f"{currency}:{project}", {"currency": currency, "project": project,
                                                                "mes_a": "0.00", "mes_b": "0.00"})[label] = value
    return {"month_a": month_a, "month_b": month_b, "analysis": billing.compare_months(report_a, report_b),
            "by_project": sorted(by_project.values(), key=lambda r: (r["currency"], r["project"]))}
