# coding: utf-8
"""Vista de portafolio: mis clientes (tenants de la plataforma) y su entorno Huawei Cloud.

Solo LECTURA de la base de datos (nunca llama a Huawei). Todas las consultas parten de
un ``client_id`` y filtran por las cuentas de ese cliente: un cliente nunca ve datos
de otro. Reutiliza el inventario existente (``resources``); no hay inventario paralelo.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Client, CloudAccount, EnterpriseProject, InventoryResource, Project
from repositories import projects as projects_repo
from repositories import resources as resources_repo
from repositories import scans as scans_repo
from tenancy.accounts import get_account, list_accounts
from tenancy.clients import get_client
from tenancy.errors import NotFoundError
from tenancy.schemas import ScanRunSummary


def _active(account_ids: List[uuid.UUID]):
    return (InventoryResource.account_id.in_(account_ids), InventoryResource.deleted_at.is_(None))


def _count_by(session: Session, account_ids: List[uuid.UUID], *columns) -> Dict[tuple, int]:
    if not account_ids:
        return {}
    rows = session.execute(select(*columns, func.count()).where(*_active(account_ids)).group_by(*columns))
    return {tuple(row[:-1]): int(row[-1]) for row in rows}


def _enterprise_projects(session: Session, account_id: uuid.UUID, *, present_only: bool = True) -> List[EnterpriseProject]:
    query = select(EnterpriseProject).where(EnterpriseProject.account_id == account_id)
    if present_only:
        query = query.where(EnterpriseProject.present.is_(True))
    return list(session.scalars(query.order_by(EnterpriseProject.name)))


def _limited_services(account: CloudAccount, session: Session) -> List[str]:
    from tenancy.iam_discovery import account_permissions  # import diferido: evita ciclo

    return [s["service"] for s in account_permissions(session, account)["services"] if s["status"] in ("denied", "partial")]


def account_card(session: Session, account: CloudAccount) -> Dict[str, Any]:
    projects = projects_repo.list_for_account(session, account.id)
    runs = scans_repo.list_for_account(session, account.id, limit=1)
    report = account.discovery_report or {}
    return {
        "id": str(account.id), "name": account.name, "status": account.status,
        "connected": account.status == "active", "last_validated_at": account.last_validated_at,
        "last_validation_error": account.last_validation_error,
        "projects": len(projects), "regions": sorted({p.region_id for p in projects}),
        "enterprise_projects": len(_enterprise_projects(session, account.id)),
        "resources": resources_repo.stats_for_account(session, account.id)["total_active"],
        "last_scan": ScanRunSummary.from_run(runs[0]).model_dump(mode="json") if runs else None,
        # AK/SK rechazadas en el último escaneo (p. ej. la clave se desactivó en Huawei después de validarla).
        "last_scan_auth_failed": bool(runs) and any(t.error_kind == "authentication" for t in scans_repo.tasks(session, runs[0].id)),
        "last_discovery_at": account.last_discovery_at,
        "discovery_steps": report.get("steps") or {},
        "limited_services": _limited_services(account, session),
    }


def client_card(session: Session, client: Client) -> Dict[str, Any]:
    accounts = [account_card(session, a) for a in list_accounts(session, client.id)]
    scans = [a["last_scan"] for a in accounts if a["last_scan"]]
    last = max(scans, key=lambda s: s.get("finished_at") or s.get("created_at") or "", default=None)
    return {
        "id": str(client.id), "name": client.name, "slug": client.slug, "status": client.status,
        "accounts": accounts,
        "connected_accounts": sum(1 for a in accounts if a["connected"]),
        "projects": sum(a["projects"] for a in accounts),
        "regions": sorted({r for a in accounts for r in a["regions"]}),
        "enterprise_projects": sum(a["enterprise_projects"] for a in accounts),
        "resources": sum(a["resources"] for a in accounts),
        "last_scan": last,
    }


def portfolio(session: Session, clients: Iterable[Client]) -> List[Dict[str, Any]]:
    return [client_card(session, c) for c in clients]


# ---------------------------------------------------------------- dentro de un cliente
def client_projects(session: Session, client_id: uuid.UUID, account_id: Optional[uuid.UUID] = None) -> List[Dict[str, Any]]:
    get_client(session, client_id)
    accounts = [get_account(session, client_id, account_id)] if account_id else list_accounts(session, client_id)
    ids = [a.id for a in accounts]
    names = {a.id: a.name for a in accounts}
    counts = _count_by(session, ids, InventoryResource.project_id)
    rows: List[Project] = list(session.scalars(select(Project).where(Project.account_id.in_(ids))
                                              .order_by(Project.region_id, Project.name))) if ids else []
    return [{"id": str(p.id), "account_id": str(p.account_id), "account": names[p.account_id],
             "huawei_project_id": p.huawei_project_id, "name": p.name, "region_id": p.region_id,
             "is_enabled": p.is_enabled, "huawei_enabled": p.huawei_enabled, "discovered_at": p.discovered_at,
             "resources": counts.get((p.id,), 0)} for p in rows]


def client_regions(session: Session, client_id: uuid.UUID, account_id: Optional[uuid.UUID] = None) -> List[Dict[str, Any]]:
    projects = client_projects(session, client_id, account_id)
    accounts = [get_account(session, client_id, account_id)] if account_id else list_accounts(session, client_id)
    counts = _count_by(session, [a.id for a in accounts], InventoryResource.region)
    regions: Dict[str, Dict[str, Any]] = {}
    for p in projects:
        entry = regions.setdefault(p["region_id"], {"region_id": p["region_id"], "projects": 0, "resources": 0})
        entry["projects"] += 1
    for (region,), count in counts.items():
        if region:  # recursos globales (OBS por cuenta) pueden traer su propia región
            regions.setdefault(region, {"region_id": region, "projects": 0, "resources": 0})["resources"] = count
    return [regions[k] for k in sorted(regions)]


def client_enterprise_projects(session: Session, client_id: uuid.UUID,
                               account_id: Optional[uuid.UUID] = None) -> List[Dict[str, Any]]:
    get_client(session, client_id)
    accounts = [get_account(session, client_id, account_id)] if account_id else list_accounts(session, client_id)
    result = []
    for account in accounts:
        counts = _count_by(session, [account.id], InventoryResource.enterprise_project_id)
        for ep in _enterprise_projects(session, account.id, present_only=False):
            result.append({"id": str(ep.id), "account_id": str(account.id), "account": account.name,
                           "huawei_ep_id": ep.huawei_ep_id, "name": ep.name, "status": ep.status,
                           "type": ep.ep_type, "present": ep.present, "discovered_at": ep.discovered_at,
                           "resources": counts.get((ep.huawei_ep_id,), 0)})
    return result


def resource_summary(session: Session, client_id: uuid.UUID, account_id: uuid.UUID, *,
                     project_id: Optional[uuid.UUID] = None, region: Optional[str] = None,
                     enterprise_project_id: Optional[str] = None) -> Dict[str, Any]:
    """Recursos activos por servicio dentro de un Project / Region / Enterprise Project."""
    account = get_account(session, client_id, account_id)
    conditions = list(_active([account.id]))
    if project_id is not None:
        if projects_repo.get_for_client(session, client_id, account.id, project_id) is None:
            raise NotFoundError("Proyecto no encontrado.")
        conditions.append(InventoryResource.project_id == project_id)
    if region:
        conditions.append(InventoryResource.region == region)
    if enterprise_project_id:
        conditions.append(InventoryResource.enterprise_project_id == enterprise_project_id)
    rows = session.execute(select(InventoryResource.service, func.count()).where(*conditions)
                           .group_by(InventoryResource.service).order_by(InventoryResource.service))
    by_service = {str(service): int(count) for service, count in rows}
    return {"account_id": str(account.id), "project_id": str(project_id) if project_id else None, "region": region,
            "enterprise_project_id": enterprise_project_id, "total": sum(by_service.values()), "by_service": by_service}
