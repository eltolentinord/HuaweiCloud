# coding: utf-8
"""ELB — Elastic Load Balance (API v3).

Paginación: ``marker`` + ``limit`` con ``page_info.next_marker``.
Sin ``enterprise_project_id`` la API ya devuelve todos los proyectos de empresa
(documentado en el SDK), por lo que no se envía.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from huaweicloudsdkelb.v3 import ElbClient, ListLoadBalancersRequest
from huaweicloudsdkelb.v3.region.elb_region import ElbRegion

from collectors.base import CollectorContext, SimpleListCollector, page_info_marker
from core.models import Resource
from core.pagination import TokenPagination
from core.serialization import as_list

PAGE_SIZE = 100


class ElbCollector(SimpleListCollector):
    service = "elb"
    resource_type = "elb.loadbalancer"
    client_cls = ElbClient
    region_cls = ElbRegion
    endpoint_prefix = "elb"
    method = "list_load_balancers"
    request_cls = ListLoadBalancersRequest
    items_field = "loadbalancers"

    def strategy(self) -> TokenPagination:
        return TokenPagination(limit=PAGE_SIZE)

    def next_token(self, response: Any) -> Optional[str]:
        return page_info_marker(response)

    def normalize(self, ctx: CollectorContext, raw: Dict[str, Any]) -> Resource:
        return self.resource(
            ctx,
            raw,
            resource_type=self.resource_type,
            status=raw.get("operating_status") or raw.get("provisioning_status"),
            attributes={
                "operating_status": raw.get("operating_status"),
                "provisioning_status": raw.get("provisioning_status"),
                "vip_address": raw.get("vip_address"),
                "vpc_id": raw.get("vpc_id"),
                "availability_zones": raw.get("availability_zone_list"),
                "guaranteed": raw.get("guaranteed"),
                "loadbalancer_type": raw.get("loadbalancer_type"),
                "public_ips": [p.get("publicip_address") for p in as_list(raw.get("publicips"))
                               if isinstance(p, dict) and p.get("publicip_address")],
            },
        )
