# coding: utf-8
"""Protección de las llamadas al SDK: límite global de concurrencia y reintentos por throttling.

Huawei Cloud limita las peticiones por API y usuario/tenant. El API Gateway responde
HTTP 429 ``APIGW.0308`` "The throttling threshold has been reached" (hay políticas
de hasta 10 peticiones/segundo por usuario) y recomienda reintentar con backoff
exponencial. Aquí:

- ``CallGate``: semáforo de PROCESO que limita las llamadas simultáneas a Huawei
  (todas las cuentas y escaneos a la vez) para no saturar ni el tenant ni la máquina.
- ``RetryPolicy``: reintenta SOLO el throttling (429/APIGW.0308) con backoff
  exponencial y jitter. Cualquier otro error se propaga sin reintentar (un 401/403
  repetido no cambia y solo añadiría carga).
- ``GuardedClient``: envuelve el cliente del SDK; los collectors no cambian.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from huaweicloudsdkcore.exceptions import exceptions as sdk_exceptions

logger = logging.getLogger(__name__)

THROTTLING_CODES = frozenset({"APIGW.0308"})
ENV_MAX_CONCURRENT_CALLS = "INVENTORY_MAX_CONCURRENT_CALLS"
DEFAULT_MAX_CONCURRENT_CALLS = 8
MAX_CONCURRENT_CALLS_CAP = 32


def is_throttling(exc: BaseException) -> bool:
    if not isinstance(exc, sdk_exceptions.ServiceResponseException):
        return False
    return getattr(exc, "status_code", None) == 429 or getattr(exc, "error_code", None) in THROTTLING_CODES


@dataclass(frozen=True)
class RetryPolicy:
    """Backoff exponencial con jitter completo: espera ∈ [0, min(cap, base·2^intento)]."""

    max_attempts: int = 4          # 1 intento + 3 reintentos
    base_delay: float = 1.0
    max_delay: float = 16.0
    sleep: Callable[[float], None] = field(default=time.sleep, compare=False, repr=False)

    def delay(self, attempt: int) -> float:
        return random.uniform(0, min(self.max_delay, self.base_delay * (2 ** attempt)))


class CallGate:
    """Límite de llamadas simultáneas a Huawei Cloud compartido por todo el proceso."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self._semaphore = threading.BoundedSemaphore(self.limit)
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def __enter__(self) -> "CallGate":
        self._semaphore.acquire()
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        return self

    def __exit__(self, *exc_info) -> None:
        with self._lock:
            self.active -= 1
        self._semaphore.release()


def _gate_limit_from_env() -> int:
    raw = (os.environ.get(ENV_MAX_CONCURRENT_CALLS) or "").strip()
    value = int(raw) if raw.isdigit() else DEFAULT_MAX_CONCURRENT_CALLS
    return min(max(value, 1), MAX_CONCURRENT_CALLS_CAP)


GLOBAL_CALL_GATE = CallGate(_gate_limit_from_env())


class RetryCounter:
    """Contador de reintentos (thread-safe) para estadísticas del escaneo."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.count = 0

    def increment(self) -> None:
        with self._lock:
            self.count += 1


def call_guarded(func: Callable[..., Any], *args: Any, policy: RetryPolicy, gate: CallGate,
                 counter: RetryCounter, label: str = "", **kwargs: Any) -> Any:
    attempt = 0
    while True:
        try:
            with gate:
                return func(*args, **kwargs)
        except sdk_exceptions.ServiceResponseException as exc:
            attempt += 1
            if not is_throttling(exc) or attempt >= policy.max_attempts:
                raise
            delay = policy.delay(attempt - 1)
            counter.increment()
            logger.warning("Throttling de Huawei Cloud en %s (intento %d/%d); reintento en %.1fs",
                           label, attempt, policy.max_attempts, delay)
            policy.sleep(delay)  # fuera del gate: no ocupa un hueco mientras espera


class GuardedClient:
    """Proxy del cliente del SDK: cada método pasa por el gate global y la política de reintento."""

    def __init__(self, client: Any, *, policy: RetryPolicy, gate: CallGate, counter: RetryCounter) -> None:
        self._client = client
        self._policy = policy
        self._gate = gate
        self._counter = counter

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._client, name)
        if not callable(attribute) or name.startswith("_") or name == "close":
            return attribute

        def guarded(*args: Any, **kwargs: Any) -> Any:
            return call_guarded(attribute, *args, policy=self._policy, gate=self._gate,
                                counter=self._counter, label=name, **kwargs)
        return guarded
