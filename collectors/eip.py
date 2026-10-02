# coding: utf-8
"""EIP — Elastic IP (API v2).

Paginación: ``marker`` = ID del último registro + ``limit``; sin total en la
respuesta. Los campos reales son ``public_ip_address`` / ``private_ip_address`` /
``create_time`` (antes se leían ``floating_ip_address``/``fixed_ip_address``,
que no existen en v2).
"""

from __future__ import annotations

from typing import Any, Dict

from huaweicloudsdkeip.v2 import EipClient, ListPublicipsRequest
from huaweicloudsdkeip.v2.region.eip_region import EipRegion

from collectors.base import CollectorContext, SimpleListCollector
from core.models import Resource
from core.pagination import MarkerPagination
from core.serialization import first_present

PAGE_SIZE = 100


class EipCollector(SimpleListCollector):
    service = "eip"
    resource_type = "eip.publicip"
    client_cls = EipClient
    region_cls = EipRegion
    endpoint_prefix = "eip"
    method = "list_publicips"
    request_cls = ListPublicipsRequest
    items_field = "publicips"

    def strategy(self) -> MarkerPagination:
        return MarkerPagination(limit=PAGE_SIZE)

    def normalize(self, ctx: CollectorContext, raw: Dict[str, Any]) -> Resource:
        public_ip = first_present(raw, "public_ip_address", "floating_ip_address", "public_ipv6_address")
        return self.resource(
            ctx,
            raw,
            resource_type=self.resource_type,
            name=first_present(raw, "alias", default=public_ip),
            created_at=first_present(raw, "create_time", "created_at"),
            attributes={
                "public_ip": public_ip,
                "private_ip": first_present(raw, "private_ip_address", "fixed_ip_address"),
                "port_id": raw.get("port_id"),
                "type": raw.get("type"),
                "ip_version": raw.get("ip_version"),
                "bandwidth_id": raw.get("bandwidth_id"),
                "bandwidth_name": raw.get("bandwidth_name"),
                "bandwidth_size": raw.get("bandwidth_size"),
                "bandwidth_share_type": raw.get("bandwidth_share_type"),
            },
        )
