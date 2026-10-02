# coding: utf-8
"""Cloud Eye (CES) — reglas de alarma (API v2).

Paginación: ``offset`` (registros, rango documentado [0, 10000]) + ``limit``
(máx. 100); respuesta con ``count``. Más de 10 100 alarmas no son alcanzables
por la API (límite del servicio, no del collector).
"""

from __future__ import annotations

from typing import Any, Dict

from huaweicloudsdkces.v2 import CesClient, ListAlarmRulesRequest
from huaweicloudsdkces.v2.region.ces_region import CesRegion

from collectors.base import CollectorContext, SimpleListCollector
from core.models import Resource
from core.pagination import OffsetPagination

PAGE_SIZE = 100  # máximo permitido por la API


class CesCollector(SimpleListCollector):
    service = "ces"
    resource_type = "ces.alarm_rule"
    client_cls = CesClient
    region_cls = CesRegion
    endpoint_prefix = "ces"
    method = "list_alarm_rules"
    request_cls = ListAlarmRulesRequest
    items_field = "alarms"
    total_field = "count"
    id_key = "alarm_id"

    def strategy(self) -> OffsetPagination:
        return OffsetPagination(limit=PAGE_SIZE)

    def normalize(self, ctx: CollectorContext, raw: Dict[str, Any]) -> Resource:
        enabled = raw.get("enabled")
        return self.resource(
            ctx,
            raw,
            resource_type=self.resource_type,
            provider_id=raw.get("alarm_id"),
            status="" if enabled is None else ("enabled" if enabled else "disabled"),
            attributes={
                "enabled": enabled,
                "namespace": raw.get("namespace"),
                "product_name": raw.get("product_name"),
                "resource_level": raw.get("resource_level"),
                "notification_enabled": raw.get("notification_enabled"),
            },
        )
