# coding: utf-8
"""EVS — Elastic Volume Service.

Paginación: ``offset`` = número de registros a saltar, ``limit`` (defecto 1000);
la respuesta trae ``count`` con el total.
El Enterprise Project ID está en ``enterprise_project_id`` (antes se leía una
clave errónea, ``os-extra_specs:marketplace_agency_key``).
"""

from __future__ import annotations

from typing import Any, Dict

from huaweicloudsdkevs.v2 import EvsClient, ListVolumesRequest
from huaweicloudsdkevs.v2.region.evs_region import EvsRegion

from collectors.base import CollectorContext, SimpleListCollector
from core.models import Resource
from core.pagination import OffsetPagination
from core.serialization import as_list, to_number

PAGE_SIZE = 100


class EvsCollector(SimpleListCollector):
    service = "evs"
    resource_type = "evs.volume"
    client_cls = EvsClient
    region_cls = EvsRegion
    endpoint_prefix = "evs"
    method = "list_volumes"
    request_cls = ListVolumesRequest
    items_field = "volumes"
    total_field = "count"

    def strategy(self) -> OffsetPagination:
        return OffsetPagination(limit=PAGE_SIZE)

    def normalize(self, ctx: CollectorContext, raw: Dict[str, Any]) -> Resource:
        attachments = [a for a in as_list(raw.get("attachments")) if isinstance(a, dict)]
        size = to_number(raw.get("size"))
        return self.resource(
            ctx,
            raw,
            resource_type=self.resource_type,
            attributes={
                "availability_zone": raw.get("availability_zone"),
                "size_gb": int(size) if size else 0,
                "volume_type": raw.get("volume_type"),
                "server_id": attachments[0].get("server_id") if attachments else None,
                "server_ids": [a.get("server_id") for a in attachments if a.get("server_id")],
                "bootable": raw.get("bootable"),
                "encrypted": raw.get("encrypted"),
                "multiattach": raw.get("multiattach"),
                "metadata": raw.get("metadata"),
                "updated_at": raw.get("updated_at"),
            },
        )
