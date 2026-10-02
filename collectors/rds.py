# coding: utf-8
"""RDS — Relational Database Service (API v3).

Paginación: ``offset`` (registros) + ``limit`` (máx. 100); respuesta con ``total_count``.
"""

from __future__ import annotations

from typing import Any, Dict

from huaweicloudsdkrds.v3 import ListInstancesRequest, RdsClient
from huaweicloudsdkrds.v3.region.rds_region import RdsRegion

from collectors.base import CollectorContext, SimpleListCollector
from core.models import Resource
from core.pagination import OffsetPagination
from core.serialization import as_dict

PAGE_SIZE = 100  # máximo permitido por la API


class RdsCollector(SimpleListCollector):
    service = "rds"
    resource_type = "rds.instance"
    client_cls = RdsClient
    region_cls = RdsRegion
    endpoint_prefix = "rds"
    method = "list_instances"
    request_cls = ListInstancesRequest
    items_field = "instances"
    total_field = "total_count"

    def strategy(self) -> OffsetPagination:
        return OffsetPagination(limit=PAGE_SIZE)

    def normalize(self, ctx: CollectorContext, raw: Dict[str, Any]) -> Resource:
        datastore = as_dict(raw.get("datastore"))
        return self.resource(
            ctx,
            raw,
            resource_type=self.resource_type,
            created_at=raw.get("created"),
            attributes={
                "engine": datastore.get("type") or raw.get("type"),
                "engine_version": datastore.get("version"),
                "instance_type": raw.get("type"),
                "vpc_id": raw.get("vpc_id"),
                "subnet_id": raw.get("subnet_id"),
                "private_ips": raw.get("private_ips"),
                "public_ips": raw.get("public_ips"),
                "port": raw.get("port"),
                "flavor": raw.get("flavor_ref"),
                "cpu": raw.get("cpu"),
                "mem": raw.get("mem"),
            },
        )
