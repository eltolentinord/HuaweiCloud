# coding: utf-8
"""Auditoría de servidores por SSH (``/api/clients/{client_id}/servers``).

- ``GET    ""``                         servidores del cliente con su última auditoría
- ``POST   ""``                         crear (contraseña cifrada)                 [credentials:manage]
- ``PUT    /{id}``                      editar (contraseña opcional: vacía = se conserva) [credentials:manage]
- ``PUT    /{id}/password``             reemplazar la contraseña                   [credentials:manage]
- ``DELETE /{id}``                      borrar (el historial de auditorías se conserva) [credentials:manage]
- ``POST   /{id}/fingerprint/reset``    aceptar una huella SSH nueva               [credentials:manage]
- ``POST   /{id}/test``                 probar SSH + sudo sin ejecutar el script   [scans:run]
- ``POST   /audits``                    auditar seleccionados o todos (202)        [scans:run]
- ``GET    /audits``                    ejecuciones (de un lote o recientes)
- ``GET    /audits/{run_id}``           detalle (JSON del script)
- ``GET    /audits/{run_id}/report.html``  HTML original del script
- ``GET    /audits/report``             comparativo html | pdf | xlsx

La contraseña nunca aparece en respuestas, logs ni auditoría.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from core.authz import Permission, Principal
from core.crypto import SecretCipher
from db.models import Server, ServerAuditRun
from db.session import get_db, get_db_session_factory
from routers.deps import get_cipher
from routers.security import get_principal, requires
from server_audit import report as reports
from server_audit import servers as servers_svc
from server_audit import service
from server_audit.runner import SshError
from tenancy import audit
from tenancy.clients import get_client
from tenancy.errors import NotFoundError, ValidationFailedError

router = APIRouter(prefix="/api/clients/{client_id}/servers", tags=["servers"])
READ = [Depends(requires(Permission.INVENTORY_READ))]
RUN = [Depends(requires(Permission.SCANS_RUN))]
MANAGE = [Depends(requires(Permission.CREDENTIALS_MANAGE))]
REPORT_TYPES = {"pdf": "application/pdf",
                "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
# Sustituible en tests (sin red): función que abre la sesión SSH.
CONNECTOR = {"connect": None}


def _connector():
    from server_audit.runner import paramiko_connect
    return CONNECTOR["connect"] or paramiko_connect


class ServerIn(BaseModel):
    name: str = Field(..., max_length=100)
    host: str = Field(..., max_length=255)
    port: int = Field(22, ge=1, le=65535)
    username: str = Field(..., max_length=64)
    password: Optional[str] = Field(None, max_length=512)
    description: Optional[str] = Field(None, max_length=500)
    enabled: bool = True


class PasswordIn(BaseModel):
    password: str = Field(..., min_length=1, max_length=512)


class AuditIn(BaseModel):
    server_ids: Optional[List[uuid.UUID]] = Field(None, max_length=200)


def _run_dict(run: Optional[ServerAuditRun], *, full: bool = False) -> Optional[Dict[str, Any]]:
    if run is None:
        return None
    out: Dict[str, Any] = {
        "id": str(run.id), "server_id": str(run.server_id) if run.server_id else None, "server_name": run.server_name,
        "batch_id": str(run.batch_id), "status": run.status, "created_at": run.created_at,
        "started_at": run.started_at, "finished_at": run.finished_at, "hostname": run.hostname, "os": run.os,
        "overall_score": run.overall_score, "hardening_pct": run.hardening_pct, "updates_pct": run.updates_pct,
        "error_kind": run.error_kind, "error_message": run.error_message, "has_html": bool(run.report_html),
        "script_sha256": run.script_sha256,
    }
    if full:
        out["result"] = run.result_json
    return out


def _server_dict(server: Server, last: Optional[ServerAuditRun]) -> Dict[str, Any]:
    return {"id": str(server.id), "name": server.name, "host": server.host, "port": server.port,
            "username": server.username, "description": server.description, "enabled": server.enabled,
            "has_password": bool(server.password_ciphertext), "host_fingerprint": server.host_fingerprint,
            "created_at": server.created_at, "updated_at": server.updated_at, "last_audit": _run_dict(last)}


@router.get("", dependencies=READ)
def list_servers(client_id: uuid.UUID, db: Session = Depends(get_db)) -> Dict[str, Any]:
    rows = servers_svc.list_servers(db, client_id)
    latest = service.latest_runs(db, client_id)
    return {"servers": [_server_dict(s, latest.get(s.id)) for s in rows], "script_sha256": service.script_sha256()}


@router.post("", status_code=201, dependencies=MANAGE)
def create_server(client_id: uuid.UUID, body: ServerIn, db: Session = Depends(get_db),
                  cipher: SecretCipher = Depends(get_cipher), actor: Principal = Depends(get_principal)):
    server = servers_svc.create_server(db, cipher, client_id=client_id, name=body.name, host=body.host,
                                       port=body.port, username=body.username, password=body.password or "",
                                       description=body.description)
    audit.record(db, actor, "server.create", client_id=client_id, target=("server", server.id),
                 details={"fields": ["name", "host", "port", "username", "password"]})
    return _server_dict(server, None)


@router.put("/{server_id}", dependencies=MANAGE)
def update_server(client_id: uuid.UUID, server_id: uuid.UUID, body: ServerIn, db: Session = Depends(get_db),
                  cipher: SecretCipher = Depends(get_cipher), actor: Principal = Depends(get_principal)):
    server = servers_svc.get_server(db, client_id, server_id)
    servers_svc.update_server(db, cipher, server, name=body.name, host=body.host, port=body.port,
                              username=body.username, password=body.password or None,
                              description=body.description, enabled=body.enabled)
    fields = ["name", "host", "port", "username", "description", "enabled"] + (["password"] if body.password else [])
    audit.record(db, actor, "server.update", client_id=client_id, target=("server", server.id),
                 details={"fields": fields})
    return _server_dict(server, service.latest_runs(db, client_id).get(server.id))


@router.put("/{server_id}/password", dependencies=MANAGE)
def replace_password(client_id: uuid.UUID, server_id: uuid.UUID, body: PasswordIn, db: Session = Depends(get_db),
                     cipher: SecretCipher = Depends(get_cipher), actor: Principal = Depends(get_principal)):
    server = servers_svc.get_server(db, client_id, server_id)
    servers_svc.replace_password(db, cipher, server, body.password)
    audit.record(db, actor, "server.password.replace", client_id=client_id, target=("server", server.id),
                 details={"fields": ["password"]})
    return {"id": str(server.id), "has_password": True}


@router.delete("/{server_id}", status_code=204, dependencies=MANAGE)
def delete_server(client_id: uuid.UUID, server_id: uuid.UUID, db: Session = Depends(get_db),
                  actor: Principal = Depends(get_principal)):
    server = servers_svc.get_server(db, client_id, server_id)
    audit.record(db, actor, "server.delete", client_id=client_id, target=("server", server.id),
                 details={"fields": ["name"]})
    db.delete(server)
    return Response(status_code=204)


@router.post("/{server_id}/fingerprint/reset", dependencies=MANAGE)
def reset_fingerprint(client_id: uuid.UUID, server_id: uuid.UUID, db: Session = Depends(get_db),
                      actor: Principal = Depends(get_principal)):
    server = servers_svc.get_server(db, client_id, server_id)
    server.host_fingerprint = None
    audit.record(db, actor, "server.fingerprint.reset", client_id=client_id, target=("server", server.id),
                 details={"fields": ["host_fingerprint"]})
    return {"id": str(server.id), "host_fingerprint": None}


@router.post("/{server_id}/test", dependencies=RUN)
def test_server(client_id: uuid.UUID, server_id: uuid.UUID, db: Session = Depends(get_db),
                cipher: SecretCipher = Depends(get_cipher)) -> Dict[str, Any]:
    server = servers_svc.get_server(db, client_id, server_id)
    password = servers_svc.decrypt_password(server, cipher)
    try:
        result = service.check_connection(server, password, connector=_connector())
    except SshError as exc:
        return {"ok": False, "error_kind": exc.kind, "message": str(exc).replace(password, "[REDACTED]")}
    if not server.host_fingerprint and result["fingerprint"]:
        server.host_fingerprint = result["fingerprint"]
    return {"ok": result["sudo"] and result["bash"], **result}


@router.post("/audits", status_code=202, dependencies=RUN)
def start_audits(client_id: uuid.UUID, body: AuditIn, background: BackgroundTasks, db: Session = Depends(get_db),
                 factory: sessionmaker = Depends(get_db_session_factory), cipher: SecretCipher = Depends(get_cipher),
                 actor: Principal = Depends(get_principal)):
    get_client(db, client_id)
    query = select(Server).where(Server.client_id == client_id, Server.enabled.is_(True))
    if body.server_ids:
        query = query.where(Server.id.in_(body.server_ids))
    targets = list(db.scalars(query.order_by(Server.name)))
    if not targets:
        raise ValidationFailedError("No hay servidores habilitados para auditar.")
    runs = service.create_batch(db, client_id, targets, requested_by=actor.subject)
    audit.record(db, actor, "server.audit.run", client_id=client_id,
                 details={"fields": [f"servers={len(runs)}"]})
    db.commit()
    background.add_task(service.run_batch, factory, cipher, [r.id for r in runs], connector=_connector())
    return {"batch_id": str(runs[0].batch_id), "runs": [_run_dict(r) for r in runs]}


@router.get("/audits", dependencies=READ)
def list_audits(client_id: uuid.UUID, batch_id: Optional[uuid.UUID] = None, server_id: Optional[uuid.UUID] = None,
                limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)) -> Dict[str, Any]:
    get_client(db, client_id)
    query = select(ServerAuditRun).where(ServerAuditRun.client_id == client_id)
    if batch_id:
        query = query.where(ServerAuditRun.batch_id == batch_id)
    if server_id:
        query = query.where(ServerAuditRun.server_id == server_id)
    runs = list(db.scalars(query.order_by(ServerAuditRun.created_at.desc()).limit(limit)))
    return {"runs": [_run_dict(r) for r in runs],
            "pending": sum(1 for r in runs if r.status in ("queued", "running"))}


def _results(db: Session, client_id: uuid.UUID, batch_id: Optional[uuid.UUID]) -> List[Dict[str, Any]]:
    if batch_id:
        runs = list(db.scalars(select(ServerAuditRun).where(
            ServerAuditRun.client_id == client_id, ServerAuditRun.batch_id == batch_id,
            ServerAuditRun.status == "succeeded")))
    else:
        runs = service.latest_successful(db, client_id)
    return [{"server_name": r.server_name, "data": r.result_json or {}} for r in runs if r.result_json]


@router.get("/audits/report", dependencies=READ)
def comparative_report(client_id: uuid.UUID, batch_id: Optional[uuid.UUID] = None,
                       format: str = Query("pdf", pattern="^(html|pdf|xlsx)$"), db: Session = Depends(get_db)):
    get_client(db, client_id)
    results = _results(db, client_id, batch_id)
    if not results:
        raise ValidationFailedError("No hay auditorías exitosas para generar el reporte.")
    now = datetime.now()
    if format == "html":
        return HTMLResponse(reports.comparative_html(results, generated_at=now),
                            headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
    builder = reports.comparative_pdf if format == "pdf" else reports.comparative_xlsx
    name = f"Auditoria_Servidores_{now:%Y%m%d_%H%M}.{format}"
    return Response(builder(results, generated_at=now), media_type=REPORT_TYPES[format],
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _run(db: Session, client_id: uuid.UUID, run_id: uuid.UUID) -> ServerAuditRun:
    run = db.scalars(select(ServerAuditRun).where(ServerAuditRun.client_id == client_id,
                                                  ServerAuditRun.id == run_id)).first()
    if run is None:
        raise NotFoundError("Auditoría no encontrada.")
    return run


@router.get("/audits/{run_id}", dependencies=READ)
def audit_detail(client_id: uuid.UUID, run_id: uuid.UUID, db: Session = Depends(get_db)) -> Dict[str, Any]:
    return _run_dict(_run(db, client_id, run_id), full=True) or {}


@router.get("/audits/{run_id}/report.html", dependencies=READ)
def audit_html(client_id: uuid.UUID, run_id: uuid.UUID, db: Session = Depends(get_db)):
    run = _run(db, client_id, run_id)
    if not run.report_html:
        raise NotFoundError("Esta auditoría no tiene reporte HTML.")
    # HTML generado por el script del usuario: se sirve aislado (sin scripts ni recursos externos).
    return HTMLResponse(run.report_html, headers={
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:"})
