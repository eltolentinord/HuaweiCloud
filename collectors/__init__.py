# coding: utf-8
"""Registro de collectors de servicios Huawei Cloud.

El orden de ``COLLECTORS`` es el orden de consulta (y de las tablas) cuando se
piden todos los servicios; coincide con el orden histórico de ``inventory.py``.
"""

from __future__ import annotations

from typing import Dict, List

from collectors.base import BaseCollector, CollectorContext
from collectors.cbr import CbrCollector
from collectors.ces import CesCollector
from collectors.cfw import CfwCollector
from collectors.dcs import DcsCollector
from collectors.ecs import EcsCollector
from collectors.eip import EipCollector
from collectors.elb import ElbCollector
from collectors.evs import EvsCollector
from collectors.hss import HssCollector
from collectors.nat import NatCollector
from collectors.obs import ObsCollector
from collectors.rds import RdsCollector
from collectors.vpc import VpcCollector
from collectors.vpn import VpnCollector
from collectors.waf import WafCollector

COLLECTORS: Dict[str, BaseCollector] = {
    c.service: c
    for c in (
        EcsCollector(),
        EvsCollector(),
        VpcCollector(),
        VpnCollector(),
        ObsCollector(),
        EipCollector(),
        CbrCollector(),
        ElbCollector(),
        RdsCollector(),
        DcsCollector(),
        NatCollector(),
        CesCollector(),
        HssCollector(),
        WafCollector(),
        CfwCollector(),
    )
}


def service_ids() -> List[str]:
    return list(COLLECTORS)


def get_collector(service: str) -> BaseCollector:
    try:
        return COLLECTORS[service]
    except KeyError:
        raise KeyError(f"Servicio no soportado: {service}") from None


__all__ = ["BaseCollector", "CollectorContext", "COLLECTORS", "get_collector", "service_ids"]
