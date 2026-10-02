# coding: utf-8
"""CBR — Cloud Backup and Recovery (vaults).

Paginación: ``offset`` (registros) + ``limit``; la respuesta trae ``count``.
"""

from __future__ import annotations

from typing import Any, Dict

from huaweicloudsdkcbr.v1 import CbrClient, ListVaultRequest
from huaweicloudsdkcbr.v1.region.cbr_region import CbrRegion

from collectors.base import CollectorContext, SimpleListCollector
from core.models import Resource
from core.pagination import OffsetPagination
from core.serialization import as_dict, as_list

PAGE_SIZE = 100


class CbrCollector(SimpleListCollector):
    service = "cbr"
    resource_type = "cbr.vault"
    client_cls = CbrClient
    region_cls = CbrRegion
    endpoint_prefix = "cbr"
    method = "list_vault"
    request_cls = ListVaultRequest
    items_field = "vaults"
    total_field = "count"

    def strategy(self) -> OffsetPagination:
        return OffsetPagination(limit=PAGE_SIZE)

    def normalize(self, ctx: CollectorContext, raw: Dict[str, Any]) -> Resource:
        billing = as_dict(raw.get("billing"))
        return self.resource(
            ctx,
            raw,
            resource_type=self.resource_type,
            status=billing.get("status"),
            attributes={
                "description": raw.get("description"),
                "availability_zone": raw.get("availability_zone"),
                "protect_type": billing.get("protect_type"),
                "object_type": billing.get("object_type"),
                "size_gb": billing.get("size"),
                "used_gb": billing.get("used"),
                "resource_count": len(as_list(raw.get("resources"))),
            },
        )
