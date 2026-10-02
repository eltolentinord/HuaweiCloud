# coding: utf-8
"""HSS — Host Security Service: servidores en HSS (API v5 ``ListHostStatus``).

API correcta (confirmada en el SDK 3.1.216): ``GET /v5/{project_id}/host-management/hosts``
("查询云服务器列表", lista de servidores). Hasta esta versión se usaba
``ListProtectionServers`` (``/v5/{project_id}/rasp/servers``), que es la lista de
servidores con protección de APLICACIONES (RASP) y que en una cuenta real respondió
``400 HSS.0002 "Failed to parse the request"``.

Paginación: ``offset`` + ``limit`` (10-200); respuesta con ``total_num``.
Proyecto de empresa: por defecto "0" (solo "default"); se envía ``all_granted_eps``
(documentado) con reintento sin el parámetro si la cuenta lo rechaza (cobertura parcial).
Sin permisos IAM sobre HSS la API responde 403 → la tarea queda ``denied``.
``resource_id`` es el ID del ECS protegido (permite cruzar HSS con el inventario ECS).
"""

from __future__ import annotations

from typing import Any, Dict

from huaweicloudsdkhss.v5 import HssClient, ListHostStatusRequest
from huaweicloudsdkhss.v5.region.hss_region import HssRegion

from collectors.base import CollectorContext, SimpleListCollector
from core.models import Resource
from core.pagination import OffsetPagination
from core.serialization import first_present

PAGE_SIZE = 100  # rango documentado: 10-200


class HssCollector(SimpleListCollector):
    service = "hss"
    # Se conserva el tipo histórico para no romper consultas ni la identidad de recursos.
    resource_type = "hss.protected_server"
    client_cls = HssClient
    region_cls = HssRegion
    endpoint_prefix = "hss"
    method = "list_host_status"
    request_cls = ListHostStatusRequest
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
                "ip": first_present(raw, "private_ip", "public_ip"),
                "private_ip": raw.get("private_ip"),
                "public_ip": raw.get("public_ip"),
                "os": first_present(raw, "os_name", "os_type"),
                "os_type": raw.get("os_type"),
                "os_version": raw.get("os_version"),
                "protect_status": raw.get("protect_status"),
                "host_status": raw.get("host_status"),
                "agent_status": raw.get("agent_status"),
                "agent_version": raw.get("agent_version"),
                "version": raw.get("version"),
                "ecs_id": raw.get("resource_id"),
                "group_name": raw.get("group_name"),
                "policy_group_name": raw.get("policy_group_name"),
                "charging_mode": raw.get("charging_mode"),
                "expire_time": raw.get("expire_time"),
            },
        )
