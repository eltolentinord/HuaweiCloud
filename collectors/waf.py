# coding: utf-8
"""WAF — instancias dedicadas (API v1).

Paginación: ``page`` (base 1) + ``pagesize``; respuesta con ``total``.
Proyecto de empresa: el valor por defecto de la API es "0" (solo "default"); se
envía ``all_granted_eps`` (documentado) con reintento sin el parámetro.
``status``/``run_status``/``access_status`` son códigos numéricos de la API.
"""

from __future__ import annotations

from typing import Any, Dict

from huaweicloudsdkwaf.v1 import ListInstanceRequest, WafClient
from huaweicloudsdkwaf.v1.region.waf_region import WafRegion

from collectors.base import CollectorContext, SimpleListCollector
from core.models import Resource
from core.pagination import PagePagination
from core.serialization import first_present

PAGE_SIZE = 100


class WafCollector(SimpleListCollector):
    service = "waf"
    resource_type = "waf.instance"
    client_cls = WafClient
    region_cls = WafRegion
    endpoint_prefix = "waf"
    method = "list_instance"
    request_cls = ListInstanceRequest
    items_field = "items"
    total_field = "total"
    all_enterprise_projects = True

    def strategy(self) -> PagePagination:
        return PagePagination(limit=PAGE_SIZE, page_param="page", size_param="pagesize", start=1)

    def normalize(self, ctx: CollectorContext, raw: Dict[str, Any]) -> Resource:
        return self.resource(
            ctx,
            raw,
            resource_type=self.resource_type,
            name=first_present(raw, "instancename", "instance_name"),
            created_at=raw.get("create_time"),
            attributes={
                "run_status": raw.get("run_status"),
                "access_status": raw.get("access_status"),
                "zone": raw.get("zone"),
                "service_ip": raw.get("service_ip"),
                "vpc_id": raw.get("vpc_id"),
                "subnet_id": raw.get("subnet_id"),
                "specification": raw.get("specification") or raw.get("resource_spec_code"),
                "server_id": raw.get("server_id"),
            },
        )
