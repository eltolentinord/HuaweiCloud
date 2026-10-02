# coding: utf-8
"""Sincroniza ``regions`` y ``service_catalog`` desde la fuente única ``core.catalog``.

``core/catalog.py`` sigue siendo la única lista escrita a mano de regiones y
servicios; la base de datos se rellena desde ahí (idempotente) y el ámbito de
cada servicio se toma del collector correspondiente.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from collectors import COLLECTORS
from core.catalog import ALL_SERVICES, REGIONES, SERVICIOS
from db.models import Region, ServiceCatalog
from repositories import catalog as repo


@dataclass
class CatalogSyncResult:
    regions_created: int = 0
    regions_updated: int = 0
    services_created: int = 0
    services_updated: int = 0


def sync_catalog(session: Session) -> CatalogSyncResult:
    result = CatalogSyncResult()
    regions = repo.regions_by_id(session)
    for order, item in enumerate(REGIONES):
        region = regions.get(item["id"])
        if region is None:
            session.add(Region(id=item["id"], display_name=item["nombre"], is_supported=True,
                               sort_order=order))
            result.regions_created += 1
        elif (region.display_name, region.is_supported, region.sort_order) != (item["nombre"], True, order):
            region.display_name, region.is_supported, region.sort_order = item["nombre"], True, order
            result.regions_updated += 1

    services = repo.services_by_id(session)
    for order, item in enumerate(s for s in SERVICIOS if s["id"] != ALL_SERVICES):
        scope = COLLECTORS[item["id"]].scope
        service = services.get(item["id"])
        if service is None:
            session.add(ServiceCatalog(id=item["id"], display_name=item["nombre"], scope=scope,
                                       sort_order=order))
            result.services_created += 1
        elif (service.display_name, service.scope, service.sort_order) != (item["nombre"], scope, order):
            service.display_name, service.scope, service.sort_order = item["nombre"], scope, order
            result.services_updated += 1
    session.flush()
    return result


def ensure_region(session: Session, region_id: str, *, endpoint_domain: str) -> Region:
    """Devuelve la región; si no existe la crea como no soportada en la interfaz."""
    region = repo.get_region(session, region_id)
    if region is None:
        region = Region(id=region_id, display_name=region_id, endpoint_domain=endpoint_domain,
                        is_supported=False, sort_order=1000)
        session.add(region)
        session.flush()
    return region
