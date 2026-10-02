# coding: utf-8
"""Control de acceso de la API interna (sin login de usuarios todavía).

Quién es el llamante (``Principal``):

1. ``INVENTORY_ADMIN_TOKEN`` definido → se exige ``Authorization: Bearer <token>``
   (comparación en tiempo constante). Principal de servicio con rol de plataforma.
2. Sin token → solo se aceptan peticiones desde loopback (127.0.0.1 / ::1). Así,
   aunque alguien arranque uvicorn con ``--host 0.0.0.0``, la API interna no queda
   expuesta a la red. Principal "local".
3. Cualquier otro caso → 401.

El login real (OIDC/JWT) sustituirá ``get_principal`` por la validación del token
del usuario; las rutas no cambian (ver docs/AUTH.md).
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Callable

from fastapi import Depends, HTTPException, Request

from core.authz import AccessDenied, Permission, Principal, ensure

logger = logging.getLogger(__name__)

ENV_ADMIN_TOKEN = "INVENTORY_ADMIN_TOKEN"
MIN_TOKEN_LENGTH = 32
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})  # testclient = TestClient en proceso


def _configured_token() -> str:
    token = (os.environ.get(ENV_ADMIN_TOKEN) or "").strip()
    if token and len(token) < MIN_TOKEN_LENGTH:
        logger.error("%s demasiado corto (mínimo %d caracteres): se ignora", ENV_ADMIN_TOKEN, MIN_TOKEN_LENGTH)
        return ""
    return token


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(status_code=401, detail=message, headers={"WWW-Authenticate": "Bearer"})


def get_principal(request: Request) -> Principal:
    token = _configured_token()
    if token:
        scheme, _, provided = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(provided.strip().encode(), token.encode()):
            raise _unauthorized("Token de administración requerido.")
        return Principal(subject="service-token", kind="service", platform_role="admin")
    host = request.client.host if request.client else ""
    if host not in LOOPBACK_HOSTS:
        logger.warning("Acceso a la API interna rechazado desde un host no local")
        raise _unauthorized("La API interna solo acepta peticiones locales o con token.")
    return Principal(subject="local", kind="local", platform_role="admin")


def require_platform_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_platform_admin:
        raise HTTPException(status_code=403, detail="Permiso insuficiente.")
    return principal


def requires(permission: Permission) -> Callable[..., Principal]:
    """Dependencia: exige ``permission`` sobre el cliente de la ruta.

    El cliente se toma de ``{client_id}`` en la ruta o, si no está, del parámetro
    de consulta ``client_id`` (p. ej. ``/api/scans/{id}?client_id=...``).
    """

    def dependency(request: Request, principal: Principal = Depends(get_principal)) -> Principal:
        client_id = request.path_params.get("client_id") or request.query_params.get("client_id")
        try:
            ensure(principal, permission, client_id)
        except AccessDenied as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
        return principal

    dependency.__name__ = f"requires_{permission.name.lower()}"
    return dependency
