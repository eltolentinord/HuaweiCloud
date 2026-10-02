# coding: utf-8
"""Conversión de objetos del SDK a estructuras JSON-safe y utilidades de valores."""

from __future__ import annotations

import datetime
import hashlib
import json
from typing import Any, Dict, Iterable, Optional

from huaweicloudsdkcore.utils.http_utils import sanitize_for_serialization

REDACTED = "[REDACTED]"

# Claves (comparadas en minúsculas) cuyo valor nunca debe conservarse en ``raw``:
# scripts de arranque, contraseñas, credenciales, tokens y cabeceras de autorización.
SENSITIVE_KEYS = frozenset(
    {
        "os-ext-srv-attr:user_data",
        "user_data",
        "adminpass",
        "admin_pass",
        "password",
        "db_user_password",
        "ak",
        "sk",
        "access_key",
        "secret_key",
        "accesskey",
        "secretkey",
        "secret",
        "private_key",
        "token",
        "auth_token",
        "access_token",
        "security_token",
        "securitytoken",
        "x-auth-token",
        "x-security-token",
        "x-subject-token",
        "authorization",
    }
)


def is_sensitive_key(key: Any) -> bool:
    return str(key).strip().lower() in SENSITIVE_KEYS


def make_serializable(obj: Any) -> Any:
    """Convierte objetos del SDK y estructuras anidadas en datos JSON-safe.

    Usa ``sanitize_for_serialization`` del SDK (respeta los nombres reales de la
    API, p. ej. ``OS-EXT-AZ:availability_zone``) y ``str()`` como último recurso.
    """
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [make_serializable(v) for v in obj]
    try:
        return make_serializable(sanitize_for_serialization(obj))
    except Exception:
        try:
            return str(obj)
        except Exception:
            return repr(obj)


def redact_sensitive(data: Any) -> Any:
    """Devuelve una copia con los valores de ``SENSITIVE_KEYS`` reemplazados."""
    if isinstance(data, dict):
        return {
            k: (REDACTED if is_sensitive_key(k) and v not in (None, "") else redact_sensitive(v))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [redact_sensitive(v) for v in data]
    return data


def redact_values(data: Any, secrets: Iterable[str]) -> Any:
    """Copia de ``data`` donde cualquier texto que contenga un secreto se redacta.

    Defensa en profundidad antes de persistir: aunque una API devolviera la AK/SK
    dentro de un campo arbitrario, nunca llegaría a la base de datos.
    """
    secrets = tuple(s for s in secrets if s)
    if not secrets:
        return data
    if isinstance(data, dict):
        return {k: redact_values(v, secrets) for k, v in data.items()}
    if isinstance(data, list):
        return [redact_values(v, secrets) for v in data]
    if isinstance(data, str):
        for secret in secrets:
            data = data.replace(secret, REDACTED)
    return data


def to_text(value: Any, default: str = "-") -> str:
    """Texto legible para tablas/Excel sin asumir el tipo del valor."""
    if value is None or value == "":
        return default
    if isinstance(value, (list, tuple, set)):
        if not value:
            return default
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        if not value:
            return default
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def to_number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def first_present(data: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Primer valor no vacío entre ``keys``."""
    if not isinstance(data, dict):
        return default
    for key in keys:
        if data.get(key) not in (None, ""):
            return data[key]
    return default


def as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def normalize_tags(value: Any) -> Dict[str, str]:
    """Unifica los distintos formatos de tags de Huawei Cloud en ``{clave: valor}``.

    Formatos soportados: ``{"k": "v"}``, ``["k=v", "k2"]`` (ECS),
    ``[{"key": "k", "value": "v"}]`` y JSON serializado en texto.
    """
    if value in (None, "", [], {}):
        return {}
    if isinstance(value, str):
        try:
            return normalize_tags(json.loads(value))
        except ValueError:
            return {value: ""}
    if isinstance(value, dict):
        return {str(k): "" if v is None else str(v) for k, v in value.items()}
    tags: Dict[str, str] = {}
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                key = first_present(item, "key", "Key", "tag_key")
                if key is not None:
                    tags[str(key)] = str(first_present(item, "value", "Value", "tag_value", default=""))
            elif isinstance(item, str):
                key, _, val = item.partition("=")
                tags[key] = val
    return tags


_DATETIME_FORMATS = ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f",
                     "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def parse_provider_datetime(value: Any) -> Optional[datetime.datetime]:
    """Fecha de creación de Huawei → ``datetime`` con zona (UTC si no trae zona).

    Formatos vistos en el SDK: ISO 8601 con/sin ``Z``, ``YYYY-MM-DD HH:MM:SS`` y
    epoch en milisegundos (WAF ``create_time``). Si no se reconoce devuelve
    ``None`` (el valor original sigue en ``raw``). SUPUESTO a confirmar con una
    cuenta real: las fechas sin zona horaria están en UTC.
    """
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        number = float(value)
        if number > 1e11:  # milisegundos
            number /= 1000
        try:
            return datetime.datetime.fromtimestamp(number, tz=datetime.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().replace("Z", "+0000")
    if len(text) > 6 and text[-3] == ":" and text[-6] in "+-":  # +08:00 → +0800
        text = text[:-3] + text[-2:]
    for fmt in _DATETIME_FORMATS:
        try:
            parsed = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)
    return None


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def fingerprint(data: Any) -> str:
    """SHA-256 estable de una estructura JSON-safe (base para detección de cambios)."""
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def serialize_items(items: Optional[Iterable[Any]]) -> list:
    """Serializa una lista del SDK descartando elementos que no son objetos."""
    return [d for d in (make_serializable(i) for i in (items or [])) if isinstance(d, dict)]
