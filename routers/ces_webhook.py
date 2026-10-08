# coding: utf-8
"""Endpoint público POST /webhook/ces-alarm.

Recibe notificaciones SMN (Cloud Eye → SMN → HTTP POST).
Responde 200 inmediatamente: el diagnóstico lo procesa el worker.

Seguridad:
  - TODO (producción): validar X-Smn-Signature (HMAC-SHA256).
  - Idempotencia: hash(alarm_id + alarm_status + fired_at) como clave única.
  - Anti-replay: fired_at debe estar dentro de ±5 minutos del reloj del servidor.
  - Nunca se devuelven secretos en la respuesta.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["webhooks"])

REPLAY_WINDOW = timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Esquemas SMN (solo campos seguros; nunca AK/SK)
# ---------------------------------------------------------------------------

class SmnAlarmDimension(BaseModel):
    name: Optional[str] = None
    value: Optional[str] = None


class SmnAlarmBody(BaseModel):
    alarm_id: str
    alarm_name: Optional[str] = None
    alarm_status: Optional[str] = None
    alarm_level: Optional[int] = None
    namespace: Optional[str] = None
    metric_name: Optional[str] = None
    threshold: Optional[float] = None
    observed_value: Optional[float] = None
    resource_id: Optional[str] = None
    fired_at: Optional[str] = None          # ISO8601 o epoch ms
    dimensions: Optional[List[SmnAlarmDimension]] = None

    model_config = {"extra": "allow"}       # SMN puede incluir campos extra


class SmnSubscriptionConfirmation(BaseModel):
    """SMN envía esto primero para confirmar la suscripción."""
    subscribe_url: Optional[str] = None
    token: Optional[str] = None
    message: Optional[str] = None

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_fired_at(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    # Intenta ISO8601 primero
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00",
                "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    # Epoch milisegundos
    try:
        ts = int(value)
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    except (ValueError, TypeError):
        pass
    return None


def _idempotency_key(alarm_id: str, alarm_status: str, fired_at: datetime) -> str:
    raw = f"{alarm_id}:{alarm_status}:{fired_at.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _safe_event(body: Dict[str, Any]) -> Dict[str, Any]:
    """Copia del cuerpo del evento sin campos sensibles."""
    BLOCKED = {"ak", "sk", "token", "password", "secret", "credential"}
    return {k: v for k, v in body.items() if k.lower() not in BLOCKED}


def _infer_alarm_type(namespace: Optional[str], metric_name: Optional[str]) -> str:
    ns = (namespace or "").upper()
    mn = (metric_name or "").lower()
    if "cpu" in mn:
        return "cpu"
    if "mem" in mn or "memory" in mn:
        return "memory"
    if "disk" in mn or "volume" in mn or "filesystem" in mn:
        return "disk"
    if "net" in mn or "bandwidth" in mn or "traffic" in mn:
        return "network"
    return "unknown"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/webhook/ces-alarm", include_in_schema=False)
async def ces_alarm_webhook(request: Request):
    """Recibe eventos de alarma CES desde SMN. Responde 200 inmediatamente."""
    # TODO (producción): validar X-Smn-Signature (HMAC-SHA256)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "reason": "invalid_json"}, status_code=400)

    # SMN envía la confirmación de suscripción antes del primer evento
    if "subscribe_url" in body or "token" in body:
        logger.info("CES webhook: confirmación de suscripción SMN recibida")
        return JSONResponse({"ok": True, "subscribed": True})

    # Parsear evento de alarma
    try:
        alarm = SmnAlarmBody.model_validate(body)
    except Exception as exc:
        return JSONResponse({"ok": False, "reason": "invalid_payload", "detail": str(exc)}, status_code=422)

    if not alarm.alarm_id:
        return JSONResponse({"ok": False, "reason": "missing_alarm_id"}, status_code=422)

    now_utc = datetime.now(timezone.utc)
    fired_at = _parse_fired_at(alarm.fired_at)
    if fired_at is None:
        fired_at = now_utc

    # Anti-replay: rechazar si fired_at está fuera de la ventana
    delta = abs((now_utc - fired_at).total_seconds())
    if delta > REPLAY_WINDOW.total_seconds():
        logger.warning("CES webhook: fired_at fuera de ventana (%.0f s). alarm_id=%s", delta, alarm.alarm_id)
        # Devolvemos 200 para que SMN no reintente; el evento queda ignorado
        return JSONResponse({"ok": True, "skipped": "replay_window"})

    ikey = _idempotency_key(alarm.alarm_id, alarm.alarm_status or "", fired_at)

    # Insertar en BD
    try:
        db = next(get_db())
        try:
            from db.models import CesAlarmEvent, DiagnosticIncident
            import uuid as _uuid

            event = CesAlarmEvent(
                alarm_id=alarm.alarm_id,
                alarm_name=alarm.alarm_name,
                namespace=alarm.namespace,
                metric_name=alarm.metric_name,
                alarm_level=alarm.alarm_level,
                resource_id=alarm.resource_id,
                alarm_status=alarm.alarm_status,
                fired_at=fired_at,
                idempotency_key=ikey,
                raw_event_safe=_safe_event(body),
                status="received",
                threshold=alarm.threshold,
                observed_value=alarm.observed_value,
            )
            db.add(event)
            db.flush()

            incident = DiagnosticIncident(
                event_id=event.id,
                ecs_instance_id=alarm.resource_id or "",
                alarm_type=_infer_alarm_type(alarm.namespace, alarm.metric_name),
                metric_name=alarm.metric_name,
                threshold=alarm.threshold,
                observed_value=alarm.observed_value,
                severity=alarm.alarm_level,
                alarm_fired_at=fired_at,
                status="alert_received",
            )
            db.add(incident)
            db.flush()

            # Vincular evento → incidente
            event.diagnostic_id = incident.id
            db.commit()
            logger.info("CES webhook: evento=%s incidente=%s alarm_id=%s",
                        event.id, incident.id, alarm.alarm_id)
        except IntegrityError:
            db.rollback()
            logger.info("CES webhook: evento duplicado (idempotency_key=%s)", ikey)
        finally:
            db.close()
    except Exception as exc:
        logger.exception("CES webhook: error al guardar evento alarm_id=%s: %s", alarm.alarm_id, exc)
        # Devolver 200 para que SMN no reintente indefinidamente
        return JSONResponse({"ok": True, "queued": False})

    return JSONResponse({"ok": True, "queued": True})
