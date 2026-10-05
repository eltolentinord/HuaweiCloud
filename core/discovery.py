# coding: utf-8
"""Descubrimiento de Project IDs con IAM ``KeystoneListAuthProjects``.

Comportamiento CONFIRMADO en el SDK ``huaweicloudsdkiam`` 3.1.216:
- ``KeystoneListAuthProjectsRequest`` no tiene parámetros ni paginación.
- Cada proyecto trae ``id``, ``name``, ``enabled``, ``parent_id``, ``domain_id``,
  ``description`` e ``is_domain``.
- Los proyectos de sistema se llaman como la región (docstring: "系统内置项目，如cn-north-4").
- En un subproyecto creado por el usuario, ``parent_id`` es el ID del proyecto
  de su región (docstring de ``parent_id``).
- La API admite el endpoint global de IAM o el de cualquier región.

Resolución de región (en este orden, sin suposiciones adicionales):
  1. ``name`` es un ID de región conocido → esa región;
  2. ``parent_id`` apunta a un proyecto de la lista resuelto por la regla 1;
  3. si no, el proyecto queda SIN región y se informa como omitido (p. ej. el
     proyecto interno "MOS" que suele aparecer). No se deduce la región por
     prefijos del nombre porque el SDK no lo documenta.

PENDIENTE DE CONFIRMAR con una cuenta real: el formato exacto de los nombres de
subproyecto y la presencia de proyectos internos en cuentas con EPS/organizaciones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Protocol, Set, Tuple

from core.clients import ClientFactory
from core.credentials import DEFAULT_REGION
from core.serialization import serialize_items

IAM_ENDPOINT_PREFIX = "iam"


@dataclass(frozen=True)
class DiscoveredProject:
    id: str
    name: str
    enabled: Optional[bool] = None
    parent_id: Optional[str] = None
    domain_id: Optional[str] = None
    description: Optional[str] = None


@dataclass(frozen=True)
class ResolvedProject:
    project: DiscoveredProject
    region_id: str  # solo se crean ResolvedProject con región resuelta


class ProjectSource(Protocol):
    """Origen de proyectos de una cuenta (IAM real o doble de prueba)."""

    def list_projects(self) -> List[DiscoveredProject]:
        ...


def sdk_region_ids() -> Set[str]:
    """Regiones declaradas por el SDK de IAM (para reconocer proyectos de sistema)."""
    from huaweicloudsdkiam.v3.region.iam_region import IamRegion

    return set(getattr(IamRegion, "static_fields", {}) or {})


class IamProjectSource:
    """Lista proyectos con ``KeystoneListAuthProjects`` (solo lectura)."""

    def __init__(self, clients: ClientFactory, *, region_id: Optional[str] = None,
                 domain_id: Optional[str] = None) -> None:
        self._clients = clients
        self._region_id = region_id or DEFAULT_REGION
        self._domain_id = domain_id

    def list_projects(self) -> List[DiscoveredProject]:
        from huaweicloudsdkiam.v3 import IamClient, KeystoneListAuthProjectsRequest
        from huaweicloudsdkiam.v3.region.iam_region import IamRegion

        client = self._clients.create_global(IamClient, IamRegion, IAM_ENDPOINT_PREFIX,
                                             self._region_id, self._domain_id)
        response = client.keystone_list_auth_projects(KeystoneListAuthProjectsRequest())
        return parse_projects(serialize_items(getattr(response, "projects", None)))


def parse_projects(items: Iterable[dict]) -> List[DiscoveredProject]:
    """Convierte la respuesta serializada en ``DiscoveredProject`` sin duplicados."""
    projects: Dict[str, DiscoveredProject] = {}
    for item in items:
        project_id, name = item.get("id"), item.get("name")
        if not project_id or not name or item.get("is_domain"):
            continue
        projects.setdefault(project_id, DiscoveredProject(
            id=str(project_id), name=str(name), enabled=item.get("enabled"),
            parent_id=item.get("parent_id") or None, domain_id=item.get("domain_id") or None,
            description=item.get("description") or None,
        ))
    return list(projects.values())


def resolve_regions(projects: Iterable[DiscoveredProject],
                    known_regions: Set[str]) -> Tuple[List[ResolvedProject], List[DiscoveredProject]]:
    """Asocia cada proyecto a su región. Devuelve ``(resueltos, omitidos)``."""
    projects = list(projects)
    system = {p.id: p.name for p in projects if p.name in known_regions}
    resolved: List[ResolvedProject] = []
    skipped: List[DiscoveredProject] = []
    for project in projects:
        region = project.name if project.name in known_regions else system.get(project.parent_id or "")
        if region:
            resolved.append(ResolvedProject(project=project, region_id=region))
        else:
            skipped.append(project)
    return resolved, skipped


# ---------------------------------------------------------------- Enterprise Projects (EPS)
# Comportamiento CONFIRMADO en el SDK ``huaweicloudsdkeps`` 3.1.216 (v1):
# - ``ListEnterpriseProject``: ``GET /v1.0/enterprise-projects``; "consulta los Enterprise
#   Projects autorizados al usuario actual". Cliente con ``GlobalCredentials``.
# - Paginación por ``offset``/``limit`` (límite 1..1000) y ``total_count`` en la respuesta.
# - ``EpDetail``: ``id`` ("0" = Enterprise Project por defecto), ``name``, ``description``,
#   ``status`` (1 habilitado, 2 deshabilitado), ``type`` (prod | poc).
# - Endpoint global ``https://eps.myhuaweicloud.com`` (región ``cn-north-4`` del SDK);
#   ``eu-west-101`` para cuentas en ``myhuaweicloud.eu``.
EPS_ENDPOINT_PREFIX = "eps"
EPS_PAGE_SIZE = 1000


@dataclass(frozen=True)
class DiscoveredEnterpriseProject:
    id: str
    name: str
    description: Optional[str] = None
    status: Optional[int] = None
    type: Optional[str] = None


class EnterpriseProjectSource(Protocol):
    def list_enterprise_projects(self) -> List[DiscoveredEnterpriseProject]:
        ...


def eps_region_for(endpoint_domain: Optional[str]) -> str:
    return "eu-west-101" if (endpoint_domain or "").endswith(".eu") else "cn-north-4"


class EpsEnterpriseProjectSource:
    """Lista Enterprise Projects con EPS ``ListEnterpriseProject`` (solo lectura)."""

    def __init__(self, clients: ClientFactory, *, endpoint_domain: Optional[str] = None,
                 domain_id: Optional[str] = None) -> None:
        self._clients = clients
        self._region_id = eps_region_for(endpoint_domain)
        self._domain_id = domain_id

    def list_enterprise_projects(self) -> List[DiscoveredEnterpriseProject]:
        from huaweicloudsdkeps.v1 import EpsClient, ListEnterpriseProjectRequest
        from huaweicloudsdkeps.v1.region.eps_region import EpsRegion

        from core.pagination import OffsetPagination, Page, paginate

        client = self._clients.create_global(EpsClient, EpsRegion, EPS_ENDPOINT_PREFIX,
                                             self._region_id, self._domain_id)

        def fetch(params: dict) -> Page:
            response = client.list_enterprise_project(ListEnterpriseProjectRequest(**params))
            return Page(items=serialize_items(getattr(response, "enterprise_projects", None)),
                        total=getattr(response, "total_count", None))

        items = paginate(fetch, OffsetPagination(limit=EPS_PAGE_SIZE), id_key="id", label="eps.enterprise-projects")
        return parse_enterprise_projects(items)


def parse_enterprise_projects(items: Iterable[dict]) -> List[DiscoveredEnterpriseProject]:
    found: Dict[str, DiscoveredEnterpriseProject] = {}
    for item in items:
        ep_id, name = item.get("id"), item.get("name")
        if ep_id is None or not name:
            continue
        status = item.get("status")
        found.setdefault(str(ep_id), DiscoveredEnterpriseProject(
            id=str(ep_id), name=str(name), description=item.get("description") or None,
            status=int(status) if isinstance(status, (int, float)) else None, type=item.get("type") or None))
    return list(found.values())
