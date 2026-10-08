# coding: utf-8
"""API de diagnósticos Cloud Eye.

Endpoints:
  GET  /api/clients/{client_id}/diagnostics            lista con paginación
  GET  /api/clients/{client_id}/diagnostics/{id}       detalle completo
  DELETE /api/clients/{client_id}/diagnostics/{id}     eliminar (solo admin)
  GET  /api/clients/{client_id}/diagnostics/{id}/pdf   descargar PDF
  GET  /api/clients/{client_id}/diagnostics/stream     SSE para estado en tiempo real
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from core.authz import Permission, Principal
from db.models import (
    DiagnosticAuditLog,
    DiagnosticIncident,
    IN_PROGRESS_STATUSES,
    CesAlarmEvent,
)
from db.session import get_db
from routers.security import get_principal, requires

# Lazy imports para evitar ciclos en modelos opcionales
def _client_name(db: Session, client_id) -> Optional[str]:
    if client_id is None:
        return None
    try:
        from db.models import Client
        c = db.get(Client, client_id)
        return c.name if c else None
    except Exception:
        return None

def _account_name(db: Session, account_id) -> Optional[str]:
    if account_id is None:
        return None
    try:
        from db.models import CloudAccount
        a = db.get(CloudAccount, account_id)
        return a.name if a else None
    except Exception:
        return None

router = APIRouter(prefix="/api/clients", tags=["diagnostics"])


# ---------------------------------------------------------------------------
# Serialización (sin secretos)
# ---------------------------------------------------------------------------

def _serialize_incident(inc: DiagnosticIncident,
                        db: Optional[Session] = None) -> Dict[str, Any]:
    return {
        "id": str(inc.id),
        "client_id": str(inc.client_id) if inc.client_id else None,
        "client_name": _client_name(db, inc.client_id) if db else None,
        "account_id": str(inc.account_id) if inc.account_id else None,
        "account_name": _account_name(db, inc.account_id) if db else None,
        "server_id": str(inc.server_id) if inc.server_id else None,
        "ecs_name": inc.ecs_name,
        "ecs_instance_id": inc.ecs_instance_id,
        "ecs_ip": inc.ecs_ip,
        "region": inc.region,
        "project_id_hw": inc.project_id_hw,
        "enterprise_project_id": inc.enterprise_project_id,
        "alarm_type": inc.alarm_type,
        "metric_name": inc.metric_name,
        "threshold": float(inc.threshold) if inc.threshold is not None else None,
        "observed_value": float(inc.observed_value) if inc.observed_value is not None else None,
        "severity": inc.severity,
        "alarm_fired_at": inc.alarm_fired_at.isoformat() if inc.alarm_fired_at else None,
        "status": inc.status,
        "possible_cause": inc.possible_cause,
        "confidence": inc.confidence,
        "reviewed": inc.reviewed,
        "duration_ms": inc.duration_ms,
        "error_kind": inc.error_kind,
        "error_message_safe": inc.error_message_safe,
        "has_pdf": inc.pdf_data is not None,
        "pdf_name": inc.pdf_name,
        "simulated": bool((inc.report_json or {}).get("simulated")),
        "created_at": inc.created_at.isoformat() if inc.created_at else None,
        "updated_at": inc.updated_at.isoformat() if inc.updated_at else None,
    }


def _serialize_incident_detail(inc: DiagnosticIncident,
                               db: Optional[Session] = None) -> Dict[str, Any]:
    base = _serialize_incident(inc, db=db)
    base["report_json"] = inc.report_json
    base["commands"] = [
        {
            "id": str(c.id),
            "sequence": c.sequence,
            "command": c.command,
            "args": c.args,
            "exit_code": c.exit_code,
            "stdout_safe": c.stdout_safe,
            "stderr_safe": c.stderr_safe,
            "duration_ms": c.duration_ms,
            "started_at": c.started_at.isoformat() if c.started_at else None,
        }
        for c in sorted(inc.commands, key=lambda x: x.sequence)
    ]
    base["evidence"] = [
        {
            "id": str(e.id),
            "kind": e.kind,
            "summary_safe": e.summary_safe,
            "data_json": e.data_json,
            "relevance_score": e.relevance_score,
        }
        for e in inc.evidence
    ]
    return base


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_incident_for_client(db: Session, client_id: uuid.UUID,
                              incident_id: uuid.UUID) -> DiagnosticIncident:
    inc = db.scalar(
        select(DiagnosticIncident)
        .where(DiagnosticIncident.id == incident_id,
               DiagnosticIncident.client_id == client_id)
    )
    if inc is None:
        raise HTTPException(status_code=404, detail="Diagnóstico no encontrado.")
    return inc


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/{client_id}/diagnostics")
def list_diagnostics(
    client_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: Optional[str] = Query(None),
    _: None = Depends(requires(Permission.DIAGNOSTICS_READ)),
) -> Dict[str, Any]:
    query = select(DiagnosticIncident).where(DiagnosticIncident.client_id == client_id)
    if status:
        query = query.where(DiagnosticIncident.status == status)

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    incidents = list(db.scalars(
        query.order_by(DiagnosticIncident.created_at.desc()).limit(limit).offset(offset)
    ))
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_serialize_incident(i, db=db) for i in incidents],
    }


@router.get("/{client_id}/diagnostics/stream")
async def stream_diagnostics(
    client_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
    _: None = Depends(requires(Permission.DIAGNOSTICS_READ)),
):
    """SSE: emite eventos de actualización de estado de incidentes activos."""
    async def event_generator():
        seen: Dict[str, str] = {}
        for _ in range(60):  # máximo 60 s de stream
            incidents = db.scalars(
                select(DiagnosticIncident)
                .where(DiagnosticIncident.client_id == client_id,
                       DiagnosticIncident.status.in_(IN_PROGRESS_STATUSES))
                .order_by(DiagnosticIncident.updated_at.desc())
                .limit(20)
            )
            for inc in incidents:
                key = str(inc.id)
                if seen.get(key) != inc.status:
                    seen[key] = inc.status
                    import json
                    data = json.dumps({"id": key, "status": inc.status})
                    yield f"data: {data}\n\n"
            await asyncio.sleep(2)
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/{client_id}/diagnostics/{incident_id}")
def get_diagnostic(
    client_id: uuid.UUID,
    incident_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
    _: None = Depends(requires(Permission.DIAGNOSTICS_READ)),
) -> Dict[str, Any]:
    inc = _get_incident_for_client(db, client_id, incident_id)
    return _serialize_incident_detail(inc, db=db)


@router.delete("/{client_id}/diagnostics/{incident_id}")
def delete_diagnostic(
    request: Request,
    client_id: uuid.UUID,
    incident_id: uuid.UUID,
    reason: Optional[str] = Query(None, max_length=500),
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
    _: None = Depends(requires(Permission.DIAGNOSTICS_DELETE)),
) -> Dict[str, Any]:
    """Elimina un diagnóstico de la plataforma. No afecta servidores ni Huawei Cloud."""
    inc = _get_incident_for_client(db, client_id, incident_id)

    if inc.status in IN_PROGRESS_STATUSES:
        raise HTTPException(
            status_code=409,
            detail={"error": "diagnostic_in_progress",
                    "message": "El diagnóstico se encuentra en ejecución y no puede eliminarse."},
        )

    # Recopilar datos para auditoría antes de eliminar el incidente
    client_name = _client_name(db, inc.client_id)
    account_name = _account_name(db, inc.account_id)
    source_ip: Optional[str] = None
    try:
        source_ip = request.client.host if request.client else None
    except Exception:
        pass
    # Obtener alarm_id desde el evento CES vinculado (no está en el incidente directamente)
    alarm_id: Optional[str] = None
    if inc.event_id is not None:
        ev = db.get(CesAlarmEvent, inc.event_id)
        alarm_id = ev.alarm_id if ev else None

    # 1. Escribir registro de auditoría permanente ANTES de borrar.
    #    Si falla, se cancela toda la operación y se devuelve error seguro.
    log = DiagnosticAuditLog(
        diagnostic_id_original=inc.id,
        account_id=inc.account_id,
        server_id=inc.server_id,
        ecs_name=inc.ecs_name,
        action="diagnostic_deleted",
        performed_by=principal.subject[:200],
        reason=reason,
        metadata_safe={
            "client_id": str(inc.client_id) if inc.client_id else None,
            "client_name": client_name,
            "account_name": account_name,
            "alarm_id": alarm_id,
            "alarm_type": inc.alarm_type,
            "metric_name": inc.metric_name,
            "ecs_instance_id": inc.ecs_instance_id,
            "region": inc.region,
            "status_at_deletion": inc.status,
            "incident_created_at": inc.created_at.isoformat() if inc.created_at else None,
            "source_ip": source_ip,
        },
    )
    db.add(log)
    try:
        db.flush()
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="No fue posible registrar la auditoría. La eliminación fue cancelada.",
        )

    # 2. Desvincular eventos CES (SET NULL, no borrar)
    from sqlalchemy import update
    db.execute(
        update(CesAlarmEvent)
        .where(CesAlarmEvent.diagnostic_id == inc.id)
        .values(diagnostic_id=None)
    )

    # 3. Borrar incidente (CASCADE sobre commands y evidence)
    db.delete(inc)
    db.commit()

    return {"success": True, "message": "El diagnóstico fue eliminado correctamente."}


@router.get("/{client_id}/diagnostics/{incident_id}/pdf")
def download_pdf(
    client_id: uuid.UUID,
    incident_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
    _: None = Depends(requires(Permission.DIAGNOSTICS_READ)),
):
    inc = _get_incident_for_client(db, client_id, incident_id)
    if not inc.pdf_data:
        raise HTTPException(status_code=404, detail="El informe PDF aún no está disponible.")
    filename = inc.pdf_name or f"diagnostico_{incident_id}.pdf"
    return Response(
        content=inc.pdf_data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
