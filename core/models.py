# coding: utf-8
"""Modelo normalizado de recursos del inventario."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.errors import ServiceError
from core.serialization import fingerprint


@dataclass
class Resource:
    """Recurso de Huawei Cloud en una representación común a todos los servicios.

    - ``attributes``: campos ya interpretados y útiles para consultas/dashboards.
    - ``raw``: respuesta serializada del SDK (con secretos redactados); se conserva
      completa para snapshots, hash y detección de cambios en fases futuras.
    """

    service: str
    resource_type: str
    provider_id: Optional[str]
    name: Optional[str]
    status: Optional[str]
    region: str
    project_id: Optional[str]
    created_at: Optional[str] = None
    enterprise_project_id: Optional[str] = None
    tags: Dict[str, str] = field(default_factory=dict)
    attributes: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def raw_hash(self) -> str:
        return fingerprint(self.raw)


@dataclass
class CollectionResult:
    """Resultado de un collector: recursos y avisos no fatales.

    ``complete=False`` indica cobertura parcial (p. ej. HSS/WAF sin
    ``all_granted_eps``): los recursos son válidos, pero la AUSENCIA de un recurso
    no prueba que se haya eliminado.
    """

    resources: List[Resource] = field(default_factory=list)
    notices: List[ServiceError] = field(default_factory=list)
    complete: bool = True
