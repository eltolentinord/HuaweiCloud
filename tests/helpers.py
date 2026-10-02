# coding: utf-8
"""Dobles de prueba: clientes del SDK simulados. Nunca hay llamadas reales."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from huaweicloudsdkcore.exceptions import exceptions as sdk_exceptions  # noqa: E402

from collectors.base import CollectorContext  # noqa: E402

FAKE_AK = "AKFAKE0000TESTONLY"
FAKE_SK = "SKFAKE0000TESTONLYSECRET"
FAKE_PROJECT = "0123456789abcdef0123456789abcdef"
REGION = "ap-southeast-3"


def api_error(status: int, message: str = "error", code: str = "TEST.0001",
              request_id: str = "req-test") -> sdk_exceptions.ClientRequestException:
    return sdk_exceptions.ClientRequestException(
        status, sdk_exceptions.SdkError(request_id, code, message)
    )


class FakeClient:
    """Cliente con métodos configurables que registra cada request recibida."""

    def __init__(self, **handlers: Callable[[Any], Any]) -> None:
        self.requests: Dict[str, List[Any]] = {}
        for method, handler in handlers.items():
            setattr(self, method, self._recorder(method, handler))

    def _recorder(self, method: str, handler: Callable[[Any], Any]) -> Callable[[Any], Any]:
        def call(request: Any) -> Any:
            self.requests.setdefault(method, []).append(request)
            return handler(request)
        return call


class FakeFactory:
    """Sustituye a ``ClientFactory``: devuelve siempre el mismo cliente falso."""

    secrets = (FAKE_AK, FAKE_SK)

    def __init__(self, client: Any) -> None:
        self.client = client

    def create(self, client_cls, region_cls, endpoint_prefix, region_id, project_id):
        return self.client

    def create_obs(self, region_id):
        return self.client


def context(client: Any) -> CollectorContext:
    return CollectorContext(clients=FakeFactory(client), region=REGION, project_id=FAKE_PROJECT)


# ------------------------------------------------------------ APIs simuladas
def ecs_api(servers: List[Dict[str, Any]]) -> Callable[[Any], Any]:
    """ECS real: ``offset`` = nº de página (0 y 1 = primera), ``limit`` = tamaño."""
    def handler(request: Any) -> Any:
        page = max(request.offset or 1, 1)
        size = request.limit or 25
        chunk = servers[(page - 1) * size: page * size]
        return SimpleNamespace(servers=chunk, count=len(servers))
    return handler


def offset_api(items: List[Dict[str, Any]], field: str, total_field: str = None,
               cap: int = None) -> Callable[[Any], Any]:
    """``offset`` = registros a saltar. ``cap`` simula un máximo inferior a ``limit``."""
    def handler(request: Any) -> Any:
        offset = request.offset or 0
        size = min(request.limit, cap) if cap else request.limit
        data = {field: items[offset: offset + size]}
        if total_field:
            data[total_field] = len(items)
        return SimpleNamespace(**data)
    return handler


def marker_api(items: List[Dict[str, Any]], field: str) -> Callable[[Any], Any]:
    """``marker`` = ID del último elemento recibido."""
    def handler(request: Any) -> Any:
        start = 0
        if request.marker:
            start = next(i for i, it in enumerate(items) if it["id"] == request.marker) + 1
        return SimpleNamespace(**{field: items[start: start + request.limit]})
    return handler


def token_api(items: List[Dict[str, Any]], field: str) -> Callable[[Any], Any]:
    """Token opaco en ``page_info.next_marker`` (ausente en la última página)."""
    def handler(request: Any) -> Any:
        start = int(request.marker.split("-")[1]) if request.marker else 0
        chunk = items[start: start + request.limit]
        end = start + len(chunk)
        page_info = SimpleNamespace(next_marker=f"tok-{end}" if end < len(items) else None,
                                    current_count=len(chunk))
        return SimpleNamespace(**{field: chunk, "page_info": page_info})
    return handler


def make_items(n: int, prefix: str = "res", **extra: Any) -> List[Dict[str, Any]]:
    return [{"id": f"{prefix}-{i:05d}", "name": f"{prefix}-{i}", "status": "ACTIVE", **extra}
            for i in range(n)]
