# coding: utf-8
"""Clasificación centralizada de errores de Huawei Cloud.

Regla de seguridad: ningún mensaje que salga de este módulo contiene AK/SK.
Los textos se limpian, se truncan y se redactan contra los secretos conocidos.

Taxonomía (``ServiceError.kind``) y gravedad:

=================  ========  ==========================================================
kind               severity  Significado / señal
=================  ========  ==========================================================
authentication     error     Credenciales inválidas. Señal: HTTP 401 con código del API
                             Gateway ``APIGW.*`` (p. ej. APIGW.0301 "Incorrect IAM
                             authentication information": AK inexistente, SK o firma
                             incorrectas) u OBS 403 ``InvalidAccessKeyId`` /
                             ``SignatureDoesNotMatch``. Afecta a TODOS los servicios.
authorization     aviso     Credenciales válidas, pero la política IAM no permite la
                             acción. Señal: 401 con código propio del servicio y/o el
                             mensaje nombra la acción IAM (p. ej. VPN.0003 "Insufficient
                             authentication for action vpn:vpnGateways:list"; la doc. de
                             VPN indica "Obtain the required permissions").
permission        aviso     HTTP 403 o mensajes AccessDenied/Forbidden: sin permiso o
                             servicio no usado/no contratado en la cuenta/región.
unavailable       aviso     La API no existe en ese endpoint/región: 404 ``APIGW.0101``
                             ("The API does not exist or has not been published").
site_mismatch     error     Se consultó el endpoint de otro sitio de Huawei Cloud: 403
                             ``CBC.0156`` ("The customer does not belong to the website
                             you are now at"). Es configuración, NO falta de permisos.
api               error     Cualquier otro error HTTP del servicio (4xx/5xx).
network           error     Conexión, DNS, TLS o timeout.
pagination        error     La API devolvió páginas inconsistentes.
internal          error     Fallo del propio collector (bug, formato inesperado).
notice            aviso     Aviso informativo emitido por un collector.
=================  ========  ==========================================================

Los avisos (``aviso``) no son fallos del inventario: el servicio se omite y el
resto continúa. Ningún error ni aviso permite marcar recursos como eliminados.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Tuple

from huaweicloudsdkcore.exceptions import exceptions as sdk_exceptions

MAX_MESSAGE_LENGTH = 500

ERROR = "error"
WARNING = "aviso"

# kinds
AUTHENTICATION = "authentication"
AUTHORIZATION = "authorization"
PERMISSION = "permission"
UNAVAILABLE = "unavailable"
API = "api"
NETWORK = "network"
PAGINATION = "pagination"
INTERNAL = "internal"
NOTICE = "notice"
SITE_MISMATCH = "site_mismatch"

WARNING_KINDS = frozenset({AUTHORIZATION, PERMISSION, UNAVAILABLE, NOTICE})
DENIED_KINDS = frozenset({AUTHORIZATION, PERMISSION})

PERMISSION_WARNING = (
    "El servicio puede no estar activo en esta cuenta/región o el usuario "
    "no tiene permisos para verlo; se omite en el resultado."
)
AUTHORIZATION_WARNING = (
    "El usuario IAM no tiene permiso para esta operación{action}; se omite el servicio. "
    "Las credenciales son válidas."
)
UNAVAILABLE_WARNING = "La API del servicio no está disponible en esta región o cuenta; se omite el servicio."
# BSS (facturación): la cuenta no pertenece al sitio cuyo endpoint se consultó.
SITE_MISMATCH_CODE = "CBC.0156"
SITE_MISMATCH_MESSAGE = (
    "El endpoint de facturación (BSS) consultado no corresponde al sitio de Huawei Cloud "
    "de esta cuenta (CBC.0156). No es un problema de permisos IAM."
)
AUTH_PREFIX = "Credenciales no válidas o sin permiso (401). Verifica tu AK/SK y la región. "
_PERMISSION_HINTS = ("accessdenied", "access denied", "forbidden")
_GATEWAY_CODE_PREFIX = "APIGW."
_API_NOT_FOUND_CODES = frozenset({"APIGW.0101"})
# OBS responde 403 (no 401) cuando la AK no existe o la firma no cuadra; son códigos de
# error oficiales de OBS que significan credenciales rechazadas, no falta de permiso.
_CREDENTIALS_REJECTED = re.compile(r"\b(InvalidAccessKeyId|SignatureDoesNotMatch)\b")
# Acción IAM "servicio:recurso:operación" (p. ej. vpn:vpnGateways:list).
_IAM_ACTION = re.compile(r"\b([a-z][a-z0-9]*:[A-Za-z0-9*]+:[A-Za-z0-9*]+)\b")
_WRAPPED_HTTP_ERROR = re.compile(
    r"status_code:(?P<status>\d{3})(?:,request_id:(?P<request>[^,}\s]*))?(?:,error_code:(?P<code>[^,}\s]*))?"
)
_NETWORK_EXCEPTIONS = (
    sdk_exceptions.ConnectionException,
    sdk_exceptions.RequestTimeoutException,
    ConnectionError,   # incluye ConnectionRefused/Reset/Aborted (sockets, DNS caído…)
    TimeoutError,
)


class PaginationError(Exception):
    """La API devolvió una secuencia de páginas inválida (bucle, formato, límite)."""


def mask_project_id(project_id: Optional[str], *, ellipsis: str = "…") -> str:
    project_id = (project_id or "").strip()
    if len(project_id) <= 10:
        return "****" + project_id[-4:] if len(project_id) > 4 else "****"
    return project_id[:6] + ellipsis + project_id[-4:]


def mask_project_id_ascii(project_id: Optional[str]) -> str:
    """Variante ASCII para logs (la consola de Windows no siempre admite "…")."""
    return mask_project_id(project_id, ellipsis="...")


def safe_message(text: Any, secrets: Iterable[str] = ()) -> str:
    """Quita etiquetas XML/HTML, redacta secretos, compacta espacios y trunca."""
    text = str(text or "Error desconocido")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())[:MAX_MESSAGE_LENGTH]


def iam_action_from(message: str) -> Optional[str]:
    match = _IAM_ACTION.search(message or "")
    return match.group(1) if match else None


@dataclass
class ServiceError:
    """Error o aviso de un servicio, listo para registrar o mostrar."""

    service: str
    kind: str  # ver tabla del módulo
    severity: str  # ERROR | WARNING
    message: str
    http_status: Optional[int] = None
    error_code: Optional[str] = None
    request_id: Optional[str] = None
    region: Optional[str] = None
    project_id_masked: Optional[str] = None
    duration_ms: Optional[int] = None
    iam_action: Optional[str] = None

    @property
    def is_warning(self) -> bool:
        return self.severity == WARNING

    @property
    def safe_explanation(self) -> Optional[str]:
        """Texto explicativo para el usuario según la categoría (o ``None``)."""
        if self.kind == PERMISSION:
            return PERMISSION_WARNING
        if self.kind == AUTHORIZATION:
            return AUTHORIZATION_WARNING.format(action=f" ({self.iam_action})" if self.iam_action else "")
        if self.kind == UNAVAILABLE:
            return UNAVAILABLE_WARNING
        if self.kind == SITE_MISMATCH:
            return SITE_MISMATCH_MESSAGE
        return None

    def to_legacy(self) -> Dict[str, Any]:
        """Formato histórico de ``inventory.consultar_servicio`` (claves en español)."""
        data: Dict[str, Any] = {
            "servicio": self.service.upper(),
            "tipo": self.severity,
            "categoria": self.kind,
            "mensaje": self.message,
        }
        if self.http_status is not None:
            data["http_status"] = str(self.http_status)
        if self.request_id:
            data["request_id"] = self.request_id
        if self.error_code:
            data["error_code"] = self.error_code
        if self.iam_action:
            data["accion_iam"] = self.iam_action
        if self.safe_explanation:
            data["mensaje_seguro"] = self.safe_explanation
        return data

    def to_log(self) -> Dict[str, Any]:
        return {
            "service": self.service,
            "kind": self.kind,
            "severity": self.severity,
            "http_status": self.http_status,
            "error_code": self.error_code,
            "request_id": self.request_id,
            "iam_action": self.iam_action,
            "region": self.region,
            "project": self.project_id_masked,
            "duration_ms": self.duration_ms,
        }


def public_error(error: Dict[str, Any]) -> Dict[str, Any]:
    """Convierte un error en formato legacy en la respuesta segura para el navegador."""
    message = safe_message(error.get("mensaje_seguro") or error.get("mensaje"))
    category = error.get("categoria")
    if category == AUTHENTICATION or (category is None and str(error.get("http_status", "")) == "401"):
        message = AUTH_PREFIX + message
    data = {
        "tipo": error.get("tipo", ERROR),
        "http_status": error.get("http_status"),
        "request_id": error.get("request_id"),
        "error_code": error.get("error_code"),
        "mensaje": message,
        "servicio": error.get("servicio"),
    }
    if category:
        data["categoria"] = category
    if error.get("accion_iam"):
        data["accion_iam"] = error["accion_iam"]
    return data


def _status_code(exc: Exception) -> Optional[int]:
    value = getattr(exc, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def classify_api_error(status: Optional[int], error_code: Optional[str], message: str) -> Tuple[str, str]:
    """``(kind, severity)`` de una respuesta HTTP de error de Huawei Cloud."""
    code = (error_code or "").strip()
    lowered = message.lower()
    if status == 401:
        if code.startswith(_GATEWAY_CODE_PREFIX) or (not code and not iam_action_from(message)):
            return AUTHENTICATION, ERROR  # el gateway rechazó AK/SK/firma
        return AUTHORIZATION, WARNING     # el servicio autenticó y la política IAM deniega
    if code == SITE_MISMATCH_CODE:
        return SITE_MISMATCH, ERROR       # endpoint de otro sitio (antes que el 403 genérico)
    if status == 403 and _CREDENTIALS_REJECTED.search(f"{code} {message}"):
        return AUTHENTICATION, ERROR
    if status == 403 or any(hint in lowered for hint in _PERMISSION_HINTS):
        return PERMISSION, WARNING
    if status == 404 and code in _API_NOT_FOUND_CODES:
        return UNAVAILABLE, WARNING
    return API, ERROR


def classify_exception(
    exc: Exception,
    *,
    service: str,
    region: Optional[str] = None,
    project_id: Optional[str] = None,
    secrets: Iterable[str] = (),
    duration_ms: Optional[int] = None,
) -> ServiceError:
    """Traduce cualquier excepción a un ``ServiceError`` sin filtrar secretos."""
    secrets = tuple(secrets)
    common: Dict[str, Any] = dict(
        service=service,
        region=region,
        project_id_masked=mask_project_id_ascii(project_id) if project_id else None,
        duration_ms=duration_ms,
    )
    if isinstance(exc, sdk_exceptions.ServiceResponseException):
        status = _status_code(exc)
        code = getattr(exc, "error_code", None) or None
        message = safe_message(getattr(exc, "error_msg", None) or exc, secrets)
        kind, severity = classify_api_error(status, code, message)
        return ServiceError(
            kind=kind,
            severity=severity,
            message=message,
            http_status=status,
            error_code=code,
            request_id=getattr(exc, "request_id", None) or None,
            iam_action=iam_action_from(message) if kind in DENIED_KINDS else None,
            **common,
        )
    if isinstance(exc, _NETWORK_EXCEPTIONS):
        message = safe_message(getattr(exc, "error_msg", None) or exc, secrets)
        return ServiceError(kind=NETWORK, severity=ERROR,
                            message=f"Error de conexión con Huawei Cloud: {message}", **common)
    if isinstance(exc, sdk_exceptions.SdkException):
        # El SDK envuelve algunos errores HTTP en un SdkException genérico, p. ej. al
        # obtener el domain_id automáticamente con GlobalCredentials (IAM):
        # "Failed to get domain id, ClientRequestException - {status_code:401,...
        #  error_code:APIGW.0301,error_msg:...}". Se clasifica por el error HTTP interno.
        message = safe_message(getattr(exc, "error_msg", None) or exc, secrets)
        wrapped = _WRAPPED_HTTP_ERROR.search(message)
        if wrapped:
            status = int(wrapped.group("status"))
            code = wrapped.group("code") or None
            kind, severity = classify_api_error(status, code, message)
            return ServiceError(kind=kind, severity=severity, message=message, http_status=status,
                                error_code=code, request_id=wrapped.group("request") or None,
                                iam_action=iam_action_from(message) if kind in DENIED_KINDS else None,
                                **common)
    if isinstance(exc, PaginationError):
        return ServiceError(kind=PAGINATION, severity=ERROR,
                            message=f"Paginación inválida: {safe_message(exc, secrets)}", **common)
    return ServiceError(
        kind=INTERNAL,
        severity=ERROR,
        message=f"Error interno al procesar la respuesta del servicio ({type(exc).__name__}).",
        **common,
    )


def notice(service: str, message: str, *, region: Optional[str] = None) -> ServiceError:
    """Aviso informativo (no fatal) emitido por un collector."""
    return ServiceError(service=service, kind=NOTICE, severity=WARNING,
                        message=safe_message(message), region=region)
