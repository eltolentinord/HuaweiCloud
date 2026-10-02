# coding: utf-8
"""Configuración de concurrencia del motor de escaneo (conservadora por defecto).

Variables de entorno (opcionales):

    INVENTORY_SCAN_MAX_WORKERS     tareas simultáneas por escaneo      (defecto 4, máx. 16)
    INVENTORY_SCAN_PER_SERVICE     tareas simultáneas del mismo servicio (defecto 2)
    INVENTORY_SCAN_PER_REGION      tareas simultáneas en la misma región (defecto 3)
    INVENTORY_MAX_CONCURRENT_CALLS llamadas simultáneas a Huawei en TODO el proceso
                                   (defecto 8, máx. 32; ver core/throttling.py)

Motivos de los valores por defecto:
- Huawei limita por API y por usuario (APIGW.0308/429; hay políticas de 10 req/s):
  ``per_service=2`` evita lanzar la misma API contra muchos proyectos a la vez.
- ``per_region=3`` reparte la carga entre endpoints regionales.
- ``max_workers=4`` mantiene bajo el uso de hilos/sockets de la máquina; con
  ``max_workers=1`` el motor se comporta exactamente como en la Fase 3A (secuencial).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

MAX_WORKERS_CAP = 16


def _int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = (environ.get(name) or "").strip()
    return int(raw) if raw.isdigit() else default


@dataclass(frozen=True)
class ScanSettings:
    max_workers: int = 4
    per_service: int = 2
    per_region: int = 3

    def __post_init__(self) -> None:
        workers = min(max(int(self.max_workers), 1), MAX_WORKERS_CAP)
        object.__setattr__(self, "max_workers", workers)
        object.__setattr__(self, "per_service", min(max(int(self.per_service), 1), workers))
        object.__setattr__(self, "per_region", min(max(int(self.per_region), 1), workers))

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None,
                 *, max_workers: Optional[int] = None) -> "ScanSettings":
        environ = os.environ if environ is None else environ
        defaults = cls()
        return cls(
            max_workers=max_workers if max_workers is not None
            else _int(environ, "INVENTORY_SCAN_MAX_WORKERS", defaults.max_workers),
            per_service=_int(environ, "INVENTORY_SCAN_PER_SERVICE", defaults.per_service),
            per_region=_int(environ, "INVENTORY_SCAN_PER_REGION", defaults.per_region),
        )

    def as_dict(self) -> dict:
        return {"max_workers": self.max_workers, "per_service": self.per_service,
                "per_region": self.per_region}
