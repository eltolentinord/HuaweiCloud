# coding: utf-8
"""Fachada de compatibilidad del inventario Huawei Cloud (solo lectura).

La lógica vive ahora en:
  - ``core/``          credenciales, clientes, paginación, errores, modelo ``Resource``;
  - ``collectors/``    un collector por servicio (15);
  - ``presentation/``  tablas/KPIs con el formato del frontend.

Este módulo conserva la interfaz histórica (``REGIONES``, ``SERVICIOS``,
``make_serializable`` y ``consultar_servicio``) para ``app.py`` y scripts.
No crea, modifica, apaga, reinicia ni elimina recursos.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from collectors import service_ids
from core.catalog import ALL_SERVICES, REGIONES, SERVICIOS
from core.clients import DEFAULT_ENDPOINT_DOMAIN
from core.credentials import CredentialProvider, HuaweiCredentials, StaticCredentialProvider
from core.engine import ServiceRun, collect_inventory
from core.errors import ERROR
from core.models import Resource
from core.serialization import make_serializable
from presentation.tables import build_service_view

__all__ = ["REGIONES", "SERVICIOS", "make_serializable", "consultar_servicio", "consultar_con_proveedor",
           "split_by_region"]


def split_by_region(run: ServiceRun, region: str) -> Tuple[List[Resource], int]:
    """Recursos a mostrar para la región consultada.

    Los servicios regionales ya vienen filtrados por la API. Los globales (OBS)
    devuelven recursos de todas las regiones: se muestran los de ``region`` y se
    informa cuántos pertenecen a otras.
    """
    if run.scope != "global":
        return run.resources, 0
    in_region = [r for r in run.resources if r.region == region]
    return in_region, len(run.resources) - len(in_region)


def _services_for(servicio: str) -> Sequence[str]:
    return service_ids() if servicio == ALL_SERVICES else [servicio]


def consultar_con_proveedor(
    servicio: str,
    provider: CredentialProvider,
    project_id: str,
    region: str,
    *,
    endpoint_domain: str = DEFAULT_ENDPOINT_DOMAIN,
) -> Dict[str, Any]:
    """Núcleo común: ejecuta los collectors con cualquier ``CredentialProvider``.

    Lo usan el flujo histórico (credenciales del formulario) y el flujo por
    ``cloud_account_id`` (credenciales cifradas en PostgreSQL).
    """
    tablas: List[Dict[str, Any]] = []
    resumen: List[Dict[str, Any]] = []
    errores: List[Dict[str, Any]] = []

    requested = list(_services_for(servicio))
    known = [s for s in requested if s in service_ids()]
    for unknown in (s for s in requested if s not in known):
        errores.append({"servicio": unknown.upper(), "tipo": ERROR, "mensaje": "Servicio no soportado"})

    runs = collect_inventory(provider, region, project_id, known, endpoint_domain=endpoint_domain)
    for run in runs:
        if run.error:
            errores.append(run.error.to_legacy())
            errores.extend(n.to_legacy() for n in run.notices)
            continue
        resources, out_of_region = split_by_region(run, region)
        service_tables, service_summary = build_service_view(run.service, resources,
                                                             out_of_region=out_of_region)
        tablas.extend(service_tables)
        resumen.extend(service_summary)
        errores.extend(n.to_legacy() for n in run.notices)

    return {"tablas": tablas, "resumen": resumen, "errores": errores}


def consultar_servicio(
    servicio: str, ak: str, sk: str, project_id: str, region: str
) -> Dict[str, Any]:
    """Devuelve ``{tablas, resumen, errores}`` para el servicio indicado o ``"todos"``.

    Flujo histórico (AK/SK del formulario). ``errores`` incluye errores
    (``tipo="error"``) y avisos (``tipo="aviso"``); nunca contienen AK/SK.
    """
    provider = StaticCredentialProvider(HuaweiCredentials(ak=ak, sk=sk))
    return consultar_con_proveedor(servicio, provider, project_id, region)
