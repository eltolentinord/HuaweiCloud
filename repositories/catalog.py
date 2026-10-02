# coding: utf-8
"""Consultas de regiones y catálogo de servicios."""

from __future__ import annotations

from typing import Dict, List, Optional, Set

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Region, ServiceCatalog


def regions_by_id(session: Session) -> Dict[str, Region]:
    return {r.id: r for r in session.scalars(select(Region))}


def region_ids(session: Session) -> Set[str]:
    return set(session.scalars(select(Region.id)))


def get_region(session: Session, region_id: str) -> Optional[Region]:
    return session.get(Region, region_id)


def services_by_id(session: Session) -> Dict[str, ServiceCatalog]:
    return {s.id: s for s in session.scalars(select(ServiceCatalog))}


def enabled_services(session: Session) -> List[ServiceCatalog]:
    return list(session.scalars(select(ServiceCatalog).where(ServiceCatalog.is_enabled.is_(True))
                                .order_by(ServiceCatalog.sort_order, ServiceCatalog.id)))
