# coding: utf-8
"""CFW — Cloud Firewall (API v1).

``ListFirewallList`` recibe la paginación en el BODY (``QueryFireWallInstanceDto``:
``offset`` en registros, ``limit`` 1-1024) y anida la respuesta en
``data.records`` con ``data.total``. Antes se iteraba ``data`` (un objeto), lo
que fallaba.

Proyecto de empresa: el SDK documenta solo un UUID concreto (o "0" sin EPS) y
NO documenta ``all_granted_eps`` para CFW, así que no se envía. Sin el
parámetro, el alcance exacto depende de la cuenta (pendiente de confirmar con
una cuenta real con EPS). Cada recurso conserva su ``enterprise_project_id``.
"""

from __future__ import annotations

from typing import Any, Dict

from huaweicloudsdkcfw.v1 import CfwClient, ListFirewallListRequest, QueryFireWallInstanceDto
from huaweicloudsdkcfw.v1.region.cfw_region import CfwRegion

from collectors.base import CollectorContext, SimpleListCollector
from core.models import Resource
from core.pagination import OffsetPagination
from core.serialization import first_present, to_text

PAGE_SIZE = 100


def build_request(**params: Any) -> ListFirewallListRequest:
    return ListFirewallListRequest(body=QueryFireWallInstanceDto(**params))


class CfwCollector(SimpleListCollector):
    service = "cfw"
    resource_type = "cfw.firewall"
    client_cls = CfwClient
    region_cls = CfwRegion
    endpoint_prefix = "cfw"
    method = "list_firewall_list"
    request_cls = staticmethod(build_request)
    items_field = "data.records"
    total_field = "data.total"
    id_key = "fw_instance_id"

    def strategy(self) -> OffsetPagination:
        return OffsetPagination(limit=PAGE_SIZE)

    def normalize(self, ctx: CollectorContext, raw: Dict[str, Any]) -> Resource:
        flavor = raw.get("flavor")
        return self.resource(
            ctx,
            raw,
            resource_type=self.resource_type,
            provider_id=first_present(raw, "fw_instance_id", "resource_id"),
            name=first_present(raw, "fw_instance_name", "name"),
            attributes={
                "resource_id": raw.get("resource_id"),
                "service_type": raw.get("service_type"),
                "ha_type": raw.get("ha_type"),
                "engine_type": raw.get("engine_type"),
                "charge_mode": raw.get("charge_mode"),
                "flavor": to_text(flavor),
            },
        )
