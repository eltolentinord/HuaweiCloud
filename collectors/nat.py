# coding: utf-8
"""NAT Gateway (API v2).

Paginación: ``marker`` = ID del último registro + ``limit`` (máx. 2000); sin total.
"""

from __future__ import annotations

from typing import Any, Dict

from huaweicloudsdknat.v2 import ListNatGatewaysRequest, NatClient
from huaweicloudsdknat.v2.region.nat_region import NatRegion

from collectors.base import CollectorContext, SimpleListCollector
from core.models import Resource
from core.pagination import MarkerPagination

PAGE_SIZE = 100


class NatCollector(SimpleListCollector):
    service = "nat"
    resource_type = "nat.gateway"
    client_cls = NatClient
    region_cls = NatRegion
    endpoint_prefix = "nat"
    method = "list_nat_gateways"
    request_cls = ListNatGatewaysRequest
    items_field = "nat_gateways"

    def strategy(self) -> MarkerPagination:
        return MarkerPagination(limit=PAGE_SIZE)

    def normalize(self, ctx: CollectorContext, raw: Dict[str, Any]) -> Resource:
        return self.resource(
            ctx,
            raw,
            resource_type=self.resource_type,
            attributes={
                "spec": raw.get("spec"),
                "router_id": raw.get("router_id"),
                "internal_network_id": raw.get("internal_network_id"),
                "ngport_ip_address": raw.get("ngport_ip_address"),
                "description": raw.get("description"),
            },
        )
