# coding: utf-8
"""DCS — Distributed Cache Service (API v2).

Paginación: ``offset`` (registros) + ``limit`` (máx. 1000); respuesta con
``instance_num``. El identificador es ``instance_id``.
"""

from __future__ import annotations

from typing import Any, Dict

from huaweicloudsdkdcs.v2 import DcsClient, ListInstancesRequest
from huaweicloudsdkdcs.v2.region.dcs_region import DcsRegion

from collectors.base import CollectorContext, SimpleListCollector
from core.models import Resource
from core.pagination import OffsetPagination

PAGE_SIZE = 100


class DcsCollector(SimpleListCollector):
    service = "dcs"
    resource_type = "dcs.instance"
    client_cls = DcsClient
    region_cls = DcsRegion
    endpoint_prefix = "dcs"
    method = "list_instances"
    request_cls = ListInstancesRequest
    items_field = "instances"
    total_field = "instance_num"
    id_key = "instance_id"

    def strategy(self) -> OffsetPagination:
        return OffsetPagination(limit=PAGE_SIZE)

    def normalize(self, ctx: CollectorContext, raw: Dict[str, Any]) -> Resource:
        return self.resource(
            ctx,
            raw,
            resource_type=self.resource_type,
            provider_id=raw.get("instance_id"),
            attributes={
                "engine": raw.get("engine"),
                "engine_version": raw.get("engine_version"),
                "spec_code": raw.get("spec_code"),
                "capacity": raw.get("capacity"),
                "port": raw.get("port"),
                "ip": raw.get("ip"),
                "availability_zones": raw.get("az_codes"),
                "vpc_id": raw.get("vpc_id"),
                "subnet_id": raw.get("subnet_id"),
            },
        )
