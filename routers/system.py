# coding: utf-8
"""Endpoints de sistema: salud, disponibilidad y métricas.

- ``GET /healthz``: el proceso responde (sin dependencias).
- ``GET /readyz``: además comprueba la base de datos si está configurada. No
  devuelve detalles del error (solo ``ok``/``error``/``not_configured``).
- ``GET /metrics``: métricas en formato Prometheus. Protegidas con el mismo
  control de acceso que la API interna (ver ``routers.security``).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from core.observability import METRICS
from db.session import DatabaseNotConfiguredError, get_engine
from core.authz import Principal
from routers.security import require_platform_admin

logger = logging.getLogger(__name__)
router = APIRouter(tags=["system"])


@router.get("/healthz")
def healthz():
    return {"status": "ok"}


@router.get("/readyz")
def readyz():
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        database = "ok"
    except DatabaseNotConfiguredError:
        database = "not_configured"
    except Exception as exc:  # se registra el tipo, nunca la URL ni el mensaje del driver
        logger.warning("readyz: base de datos no disponible (%s)", type(exc).__name__)
        database = "error"
    status = 503 if database == "error" else 200
    return JSONResponse(status_code=status, content={"status": "ok" if status == 200 else "degraded",
                                                     "database": database})


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(_: Principal = Depends(require_platform_admin)):
    return PlainTextResponse(METRICS.render(), media_type="text/plain; version=0.0.4")
