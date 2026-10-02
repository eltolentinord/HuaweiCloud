# coding: utf-8
"""VPN — gateways y conexiones (API v5).

- ``ListVgws`` no admite paginación (sin ``limit``/``marker``): una sola página.
- ``ListVpnConnections``: ``marker`` + ``limit`` con ``page_info.next_marker`` y
  ``total_count``.
La conexión no expone subredes locales; se toman del gateway (``local_subnets``)
y las remotas de ``peer_subnets``.
"""

from __future__ import annotations

from typing import Any, Dict

from huaweicloudsdkvpn.v5 import ListVgwsRequest, ListVpnConnectionsRequest, VpnClient
from huaweicloudsdkvpn.v5.region.vpn_region import VpnRegion

from collectors.base import BaseCollector, CollectorContext, page_info_marker
from core.models import CollectionResult, Resource
from core.pagination import SinglePage, TokenPagination
from core.serialization import first_present

PAGE_SIZE = 100


class VpnCollector(BaseCollector):
    service = "vpn"

    def collect(self, ctx: CollectorContext) -> CollectionResult:
        client = ctx.clients.create(VpnClient, VpnRegion, "vpn", ctx.region, ctx.project_id)
        gateways = self.list_all(client.list_vgws, ListVgwsRequest, SinglePage(),
                                 items="vpn_gateways", label="vpn.gateways")
        connections = self.list_all(
            client.list_vpn_connections, ListVpnConnectionsRequest, TokenPagination(limit=PAGE_SIZE),
            items="vpn_connections", total="total_count", next_token=page_info_marker,
            label="vpn.connections",
        )
        local_subnets = {g.get("id"): g.get("local_subnets") for g in gateways}
        resources = [self.normalize_gateway(ctx, g) for g in gateways]
        resources += [self.normalize_connection(ctx, c, local_subnets.get(c.get("vgw_id")))
                      for c in connections]
        return CollectionResult(resources=resources)

    def normalize_gateway(self, ctx: CollectorContext, raw: Dict[str, Any]) -> Resource:
        return self.resource(ctx, raw, resource_type="vpn.gateway", attributes={
            "connection_ref": first_present(raw, "connect_id", "connection_id"),
            "vpc_id": raw.get("vpc_id"),
            "local_subnets": raw.get("local_subnets"),
            "flavor": raw.get("flavor"),
            "attachment_type": raw.get("attachment_type"),
            "connection_number": raw.get("connection_number"),
            "used_connection_number": raw.get("used_connection_number"),
            "availability_zone_ids": raw.get("availability_zone_ids"),
        })

    def normalize_connection(self, ctx: CollectorContext, raw: Dict[str, Any], gateway_subnets: Any) -> Resource:
        return self.resource(ctx, raw, resource_type="vpn.connection", attributes={
            "vgw_id": raw.get("vgw_id"),
            "vgw_ip": raw.get("vgw_ip"),
            "cgw_id": raw.get("cgw_id"),
            "style": raw.get("style"),
            "local_subnets": first_present(raw, "local_subnets", default=gateway_subnets),
            "remote_subnets": first_present(raw, "remote_subnets", "peer_subnets"),
        })
