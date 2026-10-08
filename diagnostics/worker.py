# coding: utf-8
"""Worker de diagnósticos CES (``python manage.py worker-diagnostics``).

Cada ciclo (run_once):
  1. Reclama incidentes en estado alert_received / pending_diagnosis.
  2. Enriquece el incidente con client_id / account_id / region desde la BD.
  3. Si no hay servidor SSH vinculado → genera diagnóstico simulado (datos de prueba).
  4. Si hay servidor SSH → delega a diagnostics.executor.run_diagnostic (Phase B).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import sessionmaker

from core.crypto import SecretCipher
from diagnostics.queue import MAX_CLAIM, claim_pending, set_status

logger = logging.getLogger(__name__)
DEFAULT_POLL_SECONDS = 30


@dataclass
class DiagnosticsWorkerReport:
    processed: int = 0
    simulated: int = 0
    skipped_no_server: int = 0
    errors: int = 0
    incident_ids: List = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enriquecimiento de contexto tenant
# ---------------------------------------------------------------------------

def _resolve_and_enrich(session, incident) -> None:
    """Rellena client_id / account_id / region / project_id_hw / etc. desde la BD.

    Busca en ces_alarm_events el account_id, y de ahí saca CloudAccount → Client.
    Si el incidente ya tiene client_id lo deja intacto.
    """
    if incident.client_id and incident.account_id:
        return  # ya enriquecido

    from sqlalchemy import select
    from db.models import CesAlarmEvent, CloudAccount

    # Buscar el evento origen para obtener account_id
    if incident.event_id and not incident.account_id:
        event = session.get(CesAlarmEvent, incident.event_id)
        if event and event.account_id:
            incident.account_id = event.account_id

    if not incident.account_id:
        return

    account = session.get(CloudAccount, incident.account_id)
    if account is None:
        return

    if not incident.client_id:
        incident.client_id = account.client_id
    if not incident.region:
        incident.region = getattr(account, "region", "") or ""

    # project_id_hw: intentar obtener del CloudAccount o de InventoryResource
    if not incident.project_id_hw:
        incident.project_id_hw = getattr(account, "project_id", None)

    session.flush()


def _resolve_server(session, incident):
    """Busca el Server asociado al ECS instance_id del incidente.

    Busca en resources por provider_id (ECS instance_id) → obtiene la IP →
    busca un Server con ese host en el mismo cliente.
    """
    if not incident.ecs_instance_id or not incident.client_id:
        return None

    from sqlalchemy import select
    from db.models import Server, InventoryResource

    resource = session.scalar(
        select(InventoryResource)
        .where(
            InventoryResource.account_id == incident.account_id,
            InventoryResource.provider_id == incident.ecs_instance_id,
            InventoryResource.resource_type == "ecs",
        )
        .limit(1)
    )

    ip_candidates: List[str] = []
    if resource:
        attrs = resource.attributes or {}
        addresses = attrs.get("addresses") or {}
        for net_addrs in addresses.values():
            for addr in (net_addrs if isinstance(net_addrs, list) else []):
                ip = addr.get("addr") or addr.get("ip")
                if ip:
                    ip_candidates.append(ip)
        eip = attrs.get("eip") or attrs.get("floating_ip")
        if eip:
            ip_candidates.append(eip)
        # También enriquecer nombre y region desde el recurso
        if not incident.ecs_name:
            incident.ecs_name = attrs.get("name") or attrs.get("server_name") or ""
        if not incident.region:
            incident.region = resource.region or ""
        if not incident.enterprise_project_id:
            incident.enterprise_project_id = attrs.get("enterprise_project_id")

    if incident.ecs_ip:
        ip_candidates.insert(0, incident.ecs_ip)

    for ip in ip_candidates:
        server = session.scalar(
            select(Server)
            .where(
                Server.client_id == incident.client_id,
                Server.host == ip,
                Server.enabled == True,
            )
            .limit(1)
        )
        if server:
            if not incident.ecs_ip:
                incident.ecs_ip = ip
            if incident.server_id is None:
                incident.server_id = server.id
            session.flush()
            return server

    return None


# ---------------------------------------------------------------------------
# Procesamiento de un incidente
# ---------------------------------------------------------------------------

def _process_incident(factory: sessionmaker, cipher: SecretCipher,
                      incident_id) -> str:
    """Procesa un incidente. Devuelve el status final."""
    with factory() as session:
        from db.models import DiagnosticIncident

        incident = session.get(DiagnosticIncident, incident_id)
        if incident is None:
            return "not_found"

        set_status(session, incident_id, "validating")
        _resolve_and_enrich(session, incident)
        session.commit()

    with factory() as session:
        from db.models import DiagnosticIncident

        incident = session.get(DiagnosticIncident, incident_id)
        server = _resolve_server(session, incident)
        session.commit()

    if server is None:
        # Sin servidor real → diagnóstico simulado con datos de prueba
        try:
            from diagnostics.executor import run_diagnostic_simulated
            run_diagnostic_simulated(factory, incident_id)
            return "simulated"
        except Exception as exc:
            logger.warning("Diagnóstico simulado %s falló: %s", incident_id, exc)
            with factory() as session:
                set_status(session, incident_id, "failed",
                           error_kind="simulation_error",
                           error_message_safe=str(exc)[:500])
                session.commit()
            return "failed"

    # Con servidor: ejecutar diagnóstico SSH real (Phase B)
    try:
        from diagnostics.executor import run_diagnostic
        run_diagnostic(factory, cipher, incident_id)
        return "analyzed"
    except Exception as exc:
        logger.warning("Diagnóstico %s falló: %s", incident_id, exc)
        with factory() as session:
            set_status(session, incident_id, "failed",
                       error_kind="executor_error",
                       error_message_safe=str(exc)[:500])
            session.commit()
        return "failed"


# ---------------------------------------------------------------------------
# run_once / run_forever
# ---------------------------------------------------------------------------

def run_once(factory: sessionmaker, cipher: SecretCipher, *,
             max_runs: int = MAX_CLAIM) -> DiagnosticsWorkerReport:
    report = DiagnosticsWorkerReport()

    with factory() as session:
        incidents = claim_pending(session, limit=max_runs)
        ids = [i.id for i in incidents]
        for i in incidents:
            set_status(session, i.id, "validating")
        session.commit()

    for incident_id in ids:
        try:
            final = _process_incident(factory, cipher, incident_id)
            if final == "simulated":
                report.simulated += 1
            elif final == "not_found":
                pass
            else:
                report.processed += 1
            report.incident_ids.append(incident_id)
        except Exception:
            logger.exception("Error procesando incidente %s", incident_id)
            report.errors += 1

    if ids:
        logger.info(
            "Diagnostics worker: procesados=%d simulados=%d errores=%d",
            report.processed, report.simulated, report.errors,
        )
    return report


def run_forever(factory: sessionmaker, cipher: SecretCipher, *,
                poll_seconds: int = DEFAULT_POLL_SECONDS,
                stop: Optional[threading.Event] = None) -> None:
    stop = stop or threading.Event()
    logger.info("Worker de diagnósticos iniciado (cada %ds)", poll_seconds)
    while not stop.is_set():
        try:
            run_once(factory, cipher)
        except Exception:
            logger.exception("Ciclo del worker de diagnósticos fallido")
        stop.wait(poll_seconds)
    logger.info("Worker de diagnósticos detenido")
