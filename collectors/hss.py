# coding: utf-8
"""HSS — Host Security Service, servidores protegidos (API v5).

Paginación: ``offset`` + ``limit``; respuesta con ``total_num``.
Proyecto de empresa: el valor por defecto de la API es "0" (solo el proyecto
"default"). Se envía ``all_granted_eps`` (documentado en el SDK) con reintento
sin el parámetro si la cuenta lo rechaza.
Campos reales del modelo: ``private_ip``/``public_ip``, ``os_type``,
``agent_status`` (antes se leían ``host_ip``, ``os_name`` y ``host_status``).
"""

from __future__ import annotations

from typing import Any, Dict

from huaweicloudsdkhss.v5 import HssClient, ListProtectionServersRequest
from huaweicloudsdkhss.v5.region.hss_region import HssRegion

from collectors.base import CollectorContext, SimpleListCollector
from core.models import Resource
from core.pagination import OffsetPagination
from core.serialization import first_present

PAGE_SIZE = 100


class HssCollector(SimpleListCollector):
    service = "hss"
    resource_type = "hss.protected_server"
    client_cls = HssClient
    region_cls = HssRegion
    endpoint_prefix = "hss"
    method = "list_protection_servers"
    request_cls = ListProtectionServersRequest
    items_field = "data_list"
    total_field = "total_num"
    id_key = "host_id"
    all_enterprise_projects = True

    def strategy(self) -> OffsetPagination:
        return OffsetPagination(limit=PAGE_SIZE)

    def normalize(self, ctx: CollectorContext, raw: Dict[str, Any]) -> Resource:
        return self.resource(
            ctx,
            raw,
            resource_type=self.resource_type,
            provider_id=raw.get("host_id"),
            name=raw.get("host_name"),
            status=raw.get("protect_status"),
            attributes={
                "ip": first_present(raw, "private_ip", "host_ip", "public_ip"),
                "private_ip": raw.get("private_ip"),
                "public_ip": raw.get("public_ip"),
                "os": first_present(raw, "os_type", "os_name"),
                "protect_status": raw.get("protect_status"),
                "agent_status": first_present(raw, "agent_status", "host_status"),
                "agent_version": raw.get("agent_version"),
                "policy_name": raw.get("policy_name"),
                "group_name": raw.get("group_name"),
            },
        )
