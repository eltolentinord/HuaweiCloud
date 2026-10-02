# coding: utf-8
"""Observabilidad: contexto de correlación, logs estructurados y métricas básicas.

- Contexto (``contextvars``): ``request_id``, ``scan_id`` y ``task_id`` se añaden
  automáticamente a cada línea de log, también desde los hilos del motor de escaneo
  (el planificador copia el contexto al lanzar cada tarea).
- Logs: formato texto (por defecto) o JSON (``LOG_FORMAT=json``), nivel con ``LOG_LEVEL``.
  Los campos ``extra={...}`` de cada llamada se incluyen en el JSON.
- Métricas: contadores y sumas en memoria del proceso, exportadas en formato de
  texto de Prometheus (``/metrics``). Sin dependencias nuevas.

Nunca se registran secretos: los mensajes del motor ya están redactados y aquí no
se añaden cuerpos de petición ni cabeceras.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional, Tuple

request_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("request_id", default=None)
scan_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("scan_id", default=None)
task_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("task_id", default=None)

_CONTEXT_FIELDS = (("request_id", request_id_var), ("scan_id", scan_id_var), ("task_id", task_id_var))
_STANDARD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}


class ContextFilter(logging.Filter):
    """Añade request_id/scan_id/task_id a cada registro (``-`` si no hay)."""

    def filter(self, record: logging.LogRecord) -> bool:
        for name, var in _CONTEXT_FIELDS:
            if not hasattr(record, name):
                setattr(record, name, var.get() or "-")
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                data[key] = value if value != "-" else None
        if record.exc_info:
            data["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s [req=%(request_id)s scan=%(scan_id)s task=%(task_id)s]: %(message)s"


def configure_logging(level: Optional[str] = None, fmt: Optional[str] = None) -> None:
    """Configura el logger raíz una sola vez (idempotente)."""
    root = logging.getLogger()
    if getattr(root, "_inventory_configured", False):
        return
    root._inventory_configured = True  # type: ignore[attr-defined]
    if root.handlers:
        # Logging ya configurado por el entorno (tests, gestor de procesos...): no se
        # duplica la salida; solo se añade el contexto de correlación a sus handlers.
        for existing in root.handlers:
            existing.addFilter(ContextFilter())
        return
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    use_json = (fmt or os.environ.get("LOG_FORMAT", "text")).strip().lower() == "json"
    handler.setFormatter(JsonFormatter() if use_json else logging.Formatter(TEXT_FORMAT))
    root.addHandler(handler)
    root.setLevel((level or os.environ.get("LOG_LEVEL", "INFO")).upper())
    root._inventory_configured = True  # type: ignore[attr-defined]


# ----------------------------------------------------------------------- métricas
Labels = Tuple[Tuple[str, str], ...]


class Metrics:
    """Registro mínimo y thread-safe de contadores (``_total``) y sumas (``_sum``/``_count``)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, Dict[Labels, float]] = defaultdict(dict)
        self._help: Dict[str, str] = {}

    @staticmethod
    def _labels(labels: Optional[Dict[str, object]]) -> Labels:
        return tuple(sorted((k, str(v)) for k, v in (labels or {}).items()))

    def inc(self, name: str, value: float = 1.0, *, help: str = "", **labels: object) -> None:
        key = self._labels(labels)
        with self._lock:
            self._help.setdefault(name, help)
            self._counters[name][key] = self._counters[name].get(key, 0.0) + value

    def observe(self, name: str, value: float, *, help: str = "", **labels: object) -> None:
        """Duraciones: acumula ``<name>_sum`` y ``<name>_count``."""
        self.inc(f"{name}_sum", value, help=help, **labels)
        self.inc(f"{name}_count", 1, help=help, **labels)

    def value(self, name: str, **labels: object) -> float:
        with self._lock:
            return self._counters.get(name, {}).get(self._labels(labels), 0.0)

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()

    def render(self) -> str:
        """Formato de exposición de Prometheus (text/plain; version=0.0.4)."""
        with self._lock:
            snapshot = {name: dict(values) for name, values in self._counters.items()}
        lines = []
        for name in sorted(snapshot):
            kind = "counter" if name.endswith("_total") else "untyped"
            if self._help.get(name):
                lines.append(f"# HELP {name} {self._help[name]}")
            lines.append(f"# TYPE {name} {kind}")
            for labels, value in sorted(snapshot[name].items()):
                rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
                lines.append(f"{name}{{{rendered}}} {value:g}" if rendered else f"{name} {value:g}")
        return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


METRICS = Metrics()


def iter_context() -> Iterator[Tuple[str, Optional[str]]]:
    for name, var in _CONTEXT_FIELDS:
        yield name, var.get()
