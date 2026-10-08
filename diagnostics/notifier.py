# coding: utf-8
"""Notificador de diagnósticos completados.

Actualmente registra el enlace al informe en el log.
TODO: integrar con SMN o email cuando se configure un endpoint de notificación.
"""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)


def notify_report_ready(incident_id: uuid.UUID, client_id: uuid.UUID) -> None:
    """Notifica que el informe de diagnóstico está disponible.

    El enlace al informe es:
      GET /api/clients/{client_id}/diagnostics/{incident_id}
      GET /api/clients/{client_id}/diagnostics/{incident_id}/pdf
    """
    # TODO: enviar notificación por SMN o email si se configura DIAGNOSTIC_NOTIFY_URL
    logger.info(
        "Informe de diagnóstico disponible: incident_id=%s client_id=%s "
        "endpoint=/api/clients/%s/diagnostics/%s",
        incident_id, client_id, client_id, incident_id,
    )
