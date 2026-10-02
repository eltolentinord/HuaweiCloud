# coding: utf-8
"""Middlewares HTTP: request ID, cabeceras de seguridad, métricas, CORS y errores 500 seguros."""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from typing import List

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from core.observability import METRICS, request_id_var

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
ENV_CORS_ORIGINS = "INVENTORY_CORS_ORIGINS"

# La interfaz usa Tailwind CDN, Google Fonts, lucide (unpkg) y Chart.js (jsdelivr).
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://unpkg.com https://cdn.jsdelivr.net",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com data:",
    "img-src 'self' data:",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
])
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
}


def _route_label(request: Request) -> str:
    """Plantilla de la ruta (``/api/clients/{client_id}/…``): evita cardinalidad infinita."""
    route = request.scope.get("route")
    return getattr(route, "path", None) or "unmatched"


def cors_origins_from_env() -> List[str]:
    """Orígenes explícitos; ``*`` se rechaza (la API maneja datos de clientes)."""
    raw = os.environ.get(ENV_CORS_ORIGINS, "")
    origins = [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]
    if "*" in origins:
        logger.warning("%s='*' ignorado: indica orígenes explícitos", ENV_CORS_ORIGINS)
        origins = [o for o in origins if o != "*"]
    return origins


def install_middlewares(app: FastAPI) -> None:
    origins = cors_origins_from_env()
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False,
                           allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
                           allow_headers=["Authorization", "Content-Type", REQUEST_ID_HEADER],
                           expose_headers=[REQUEST_ID_HEADER])

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:  # no debería llegar aquí (hay manejador de 500), pero nunca filtra detalles
            logger.exception("Error no controlado en %s %s", request.method, _route_label(request))
            response = JSONResponse(status_code=500, content={"detail": "Error interno del servidor.",
                                                              "request_id": request_id})
        finally:
            request_id_var.reset(token)
        elapsed = time.perf_counter() - started
        route = _route_label(request)
        METRICS.inc("http_requests_total", help="Peticiones HTTP", method=request.method, route=route,
                    status=response.status_code)
        METRICS.observe("http_request_duration_seconds", elapsed, help="Duración de peticiones HTTP",
                        method=request.method, route=route)
        response.headers[REQUEST_ID_HEADER] = request_id
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")  # datos de inventario
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        # Nunca se devuelve el texto de la excepción: puede contener datos internos.
        logger.error("Error no controlado en %s %s (%s)", request.method, _route_label(request),
                     type(exc).__name__, exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Error interno del servidor.",
                                                      "request_id": request_id_var.get()})
