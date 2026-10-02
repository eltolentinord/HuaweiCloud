# coding: utf-8
"""Fuentes OFICIALES de Huawei Cloud para el comparador (solo consultas, nada se crea).

Confirmado en ``huaweicloudsdkbss`` 3.1.216:
- ``ListOnDemandResourceRatings``  ``POST /v2/bills/ratings/on-demand-resources``
  (precio de pago por uso; aquí por 1 hora: ``usage_factor=Duration``,
  ``usage_value=1``, ``usage_measure_id=4`` = hora).
- ``ListRateOnPeriodDetail``       ``POST /v2/bills/ratings/period-resources/subscribe-rate``
  (precio de suscripción; aquí 1 mes: ``period_type=2``, ``period_num=1``).
- Ambas exigen el ``project_id`` de la región cotizada y devuelven el precio de lista
  ``official_website_amount`` con ``measure_id`` (1 = unidad monetaria) y ``currency``
  (vacío = CNY según el SDK).
- ``resource_spec`` (docstring del SDK): ECS = ``<flavor>.linux|.win``; EVS = SATA/SAS/
  GPSSD/SSD con ``resource_size`` en GB (``size_measure_id=17``); IP = ``5_bgp``…;
  ancho de banda por tamaño = ``19_bgp``… con Mbps (``size_measure_id=15``).

Confirmado en ``huaweicloudsdkecs`` (v2): ``ListFlavors`` ``GET /v1/{project_id}/cloudservers/flavors``.

PENDIENTE DE VALIDAR con una cuenta real: los códigos ``cloud_service_type`` /
``resource_type`` de ``PRODUCT_CODES`` (el SDK solo documenta como ejemplo
``hws.resource.type.vm`` y ``hws.service.type.obs``), el endpoint BSS para cuentas
internacionales (igual que ``costs.billing``) y los permisos IAM. Si Huawei rechaza
una consulta, el componente queda "precio no disponible" con el motivo.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.clients import ClientFactory
from core.errors import classify_exception
from core.validation import validate_region_id
from costs.billing import default_client_builder as default_bss_builder
from costs.catalog import Flavor, Quote
from costs.configuration import Component

PRODUCT_CODES: Dict[str, Tuple[str, str, Optional[int]]] = {
    # producto: (cloud_service_type, resource_type, size_measure_id)
    "ecs": ("hws.service.type.ec2", "hws.resource.type.vm", None),
    "evs": ("hws.service.type.ebs", "hws.resource.type.volume", 17),
    "ip": ("hws.service.type.vpc", "hws.resource.type.ip", None),
    "bandwidth": ("hws.service.type.vpc", "hws.resource.type.bandwidth", 15),
}
HOUR_MEASURE_ID = 4
MONTH_PERIOD_TYPE = 2
MONEY_MEASURE_ID = 1


class PricingDataError(ValueError):
    """Respuesta de precios inesperada (no se interpreta para no inventar)."""


def _resource_spec(component: Component) -> str:
    # El SDK documenta ".win" para Windows; el modelo interno usa "windows".
    spec = component.spec or ""
    return spec[:-len(".windows")] + ".win" if component.product == "ecs" and spec.endswith(".windows") else spec


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise PricingDataError("importe no numérico en la respuesta de precios") from None


def _product_kwargs(component: Component, region: str) -> Dict[str, Any]:
    service_type, resource_type, size_measure = PRODUCT_CODES[component.product]
    kwargs: Dict[str, Any] = {"id": "1", "cloud_service_type": service_type, "resource_type": resource_type,
                              "resource_spec": _resource_spec(component), "region": region, "subscription_num": 1}
    if size_measure is not None:
        kwargs.update(resource_size=component.size, size_measure_id=size_measure)
    return kwargs


def quote_component(client: Any, *, project_id: str, region: str, component: Component,
                    billing_mode: str, now: Optional[datetime] = None) -> Quote:
    """Precio oficial de UN componente (una consulta por componente: un código
    rechazado no invalida el resto)."""
    from huaweicloudsdkbss.v2 import (
        DemandProductInfo,
        ListOnDemandResourceRatingsRequest,
        ListRateOnPeriodDetailRequest,
        PeriodProductInfo,
        RateOnDemandReq,
        RateOnPeriodReq,
    )

    now = now or datetime.now(timezone.utc)
    product = _product_kwargs(component, region)
    if billing_mode == "on_demand":
        response = client.list_on_demand_resource_ratings(ListOnDemandResourceRatingsRequest(body=RateOnDemandReq(
            project_id=project_id, inquiry_precision=1, product_infos=[DemandProductInfo(
                usage_factor="Duration", usage_value=1, usage_measure_id=HOUR_MEASURE_ID, **product)])))
        amount, measure = getattr(response, "official_website_amount", None), getattr(response, "measure_id", None)
        period, detail = "hour", "ListOnDemandResourceRatings"
    else:
        response = client.list_rate_on_period_detail(ListRateOnPeriodDetailRequest(body=RateOnPeriodReq(
            project_id=project_id, product_infos=[PeriodProductInfo(
                period_type=MONTH_PERIOD_TYPE, period_num=1, **product)])))
        result = getattr(response, "official_website_rating_result", None)
        amount, measure = getattr(result, "official_website_amount", None), getattr(result, "measure_id", None)
        period, detail = "month", "ListRateOnPeriodDetail"
    if amount is None:
        raise PricingDataError("la respuesta no incluye official_website_amount")
    if measure not in (None, MONEY_MEASURE_ID):
        raise PricingDataError(f"unidad de importe no soportada (measure_id={measure})")
    return Quote(region=region, product=component.product, spec=component.spec or "", size=component.size,
                 billing_mode=billing_mode, amount=_decimal(amount), period=period,
                 currency=(getattr(response, "currency", None) or "CNY").upper()[:8], fetched_at=now,
                 source_detail=f"{detail}, official_website_amount")


def fetch_quotes(clients: ClientFactory, *, project_id: str, region: str, components: List[Component],
                 billing_mode: str, client_builder: Callable[[ClientFactory], Any] = default_bss_builder,
                 ) -> Tuple[List[Quote], List[Dict[str, str]]]:
    """Precios oficiales de varios componentes. Devuelve ``(precios, fallos_seguros)``."""
    region = validate_region_id(region)
    client = client_builder(clients)
    quotes: List[Quote] = []
    failures: List[Dict[str, str]] = []
    for component in components:
        try:
            quotes.append(quote_component(client, project_id=project_id, region=region,
                                          component=component, billing_mode=billing_mode))
        except PricingDataError as exc:
            failures.append({"component": component.label, "region": region, "reason": str(exc)})
        except Exception as exc:  # clasificado y redactado: nunca AK/SK
            error = classify_exception(exc, service="bss", region=region, secrets=clients.secrets)
            failures.append({"component": component.label, "region": region, "reason": error.message})
    return quotes, failures


def default_ecs_builder(clients: ClientFactory, region: str, project_id: str) -> Any:
    from huaweicloudsdkecs.v2 import EcsClient
    from huaweicloudsdkecs.v2.region.ecs_region import EcsRegion

    return clients.create(EcsClient, EcsRegion, "ecs", region, project_id)


def fetch_flavors(clients: ClientFactory, *, region: str, project_id: str,
                  client_builder: Callable[[ClientFactory, str, str], Any] = default_ecs_builder) -> List[Flavor]:
    """Flavors ECS vendibles en la región (excluye los marcados ``abandon``). Lanza las excepciones del SDK."""
    from huaweicloudsdkecs.v2 import ListFlavorsRequest

    region = validate_region_id(region)
    response = client_builder(clients, region, project_id).list_flavors(ListFlavorsRequest())
    flavors: List[Flavor] = []
    for item in getattr(response, "flavors", None) or []:
        specs = getattr(item, "os_extra_specs", None)
        if getattr(specs, "condoperationstatus", None) == "abandon":
            continue
        try:
            vcpus, ram = int(str(getattr(item, "vcpus", ""))), int(getattr(item, "ram", 0))
        except (TypeError, ValueError):
            continue
        flavor_id = str(getattr(item, "id", "") or getattr(item, "name", ""))[:64]
        if flavor_id:
            flavors.append(Flavor(flavor_id=flavor_id, vcpus=vcpus, ram_mb=ram,
                                  performance_type=getattr(specs, "ecsperformancetype", None),
                                  generation=getattr(specs, "ecsgeneration", None)))
    return flavors
