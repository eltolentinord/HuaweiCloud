# coding: utf-8
"""VPC — VPCs, subredes y security groups (API v3).

Paginación: ``marker`` + ``limit``; la API devuelve ``page_info.next_marker``
(ausente en la última página).
En v3 la subred no tiene ``cidr``/``gateway_ip``/``availability_zone`` en la raíz:
vienen en ``subnet_cidrs[]`` y ``zone_id``. Se aceptan ambos formatos.
"""

from __future__ import annotations

from typing import Any, Dict, List

from huaweicloudsdkvpc.v3 import (
    ListSecurityGroupsRequest,
    ListVirsubnetsRequest,
    ListVpcsRequest,
    VpcClient,
)
from huaweicloudsdkvpc.v3.region.vpc_region import VpcRegion

from collectors.base import BaseCollector, CollectorContext, page_info_marker
from core.models import CollectionResult, Resource
from core.pagination import TokenPagination
from core.serialization import as_dict, as_list, first_present

PAGE_SIZE = 100


def _ipv4_cidr(raw: Dict[str, Any]) -> Dict[str, Any]:
    cidrs = [c for c in as_list(raw.get("subnet_cidrs")) if isinstance(c, dict)]
    ipv4 = next((c for c in cidrs if str(c.get("ip_version", "4")) == "4"), cidrs[0] if cidrs else {})
    return as_dict(ipv4)


class VpcCollector(BaseCollector):
    service = "vpc"

    def _list(self, client: Any, method: str, request_cls: Any, field: str) -> List[Dict[str, Any]]:
        return self.list_all(
            getattr(client, method),
            request_cls,
            TokenPagination(limit=PAGE_SIZE),
            items=field,
            next_token=page_info_marker,
            label=f"vpc.{field}",
        )

    def collect(self, ctx: CollectorContext) -> CollectionResult:
        client = ctx.clients.create(VpcClient, VpcRegion, "vpc", ctx.region, ctx.project_id)
        resources = [self.normalize_vpc(ctx, r) for r in self._list(client, "list_vpcs", ListVpcsRequest, "vpcs")]
        resources += [self.normalize_subnet(ctx, r)
                      for r in self._list(client, "list_virsubnets", ListVirsubnetsRequest, "virsubnets")]
        resources += [self.normalize_security_group(ctx, r)
                      for r in self._list(client, "list_security_groups", ListSecurityGroupsRequest, "security_groups")]
        return CollectionResult(resources=resources)

    def normalize_vpc(self, ctx: CollectorContext, raw: Dict[str, Any]) -> Resource:
        return self.resource(ctx, raw, resource_type="vpc.vpc", attributes={
            "cidr": raw.get("cidr"),
            "extend_cidrs": raw.get("extend_cidrs"),
            "description": raw.get("description"),
            "updated_at": raw.get("updated_at"),
        })

    def normalize_subnet(self, ctx: CollectorContext, raw: Dict[str, Any]) -> Resource:
        cidr = _ipv4_cidr(raw)
        return self.resource(ctx, raw, resource_type="vpc.subnet", attributes={
            "cidr": first_present(raw, "cidr", default=cidr.get("cidr")),
            "gateway_ip": first_present(raw, "gateway_ip", default=cidr.get("gateway_ip")),
            "availability_zone": first_present(raw, "availability_zone", "zone_id"),
            "vpc_id": raw.get("vpc_id"),
            "dhcp_enabled": first_present(raw, "dhcp_enable", "dhcp_enabled", default=cidr.get("enable_dhcp")),
            "scope": raw.get("scope"),
            "description": raw.get("description"),
        })

    def normalize_security_group(self, ctx: CollectorContext, raw: Dict[str, Any]) -> Resource:
        return self.resource(ctx, raw, resource_type="vpc.security_group", attributes={
            "description": raw.get("description"),
            "updated_at": raw.get("updated_at"),
        })
