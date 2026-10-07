# coding: utf-8
"""Fuentes OFICIALES de Huawei Cloud para el comparador (solo consultas, nada se crea).

Confirmado en ``huaweicloudsdkbss`` / ``huaweicloudsdkbssintl`` 3.1.216 (mismas rutas y modelos):
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
``hws.resource.type.vm`` y ``hws.service.type.obs``) y los permisos IAM. El endpoint
BSS es el del sitio de la cuenta (``core.bss``, igual que ``costs.billing``). Si Huawei
rechaza una consulta, el componente queda "precio no disponible" con el motivo.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.bss import bss_models
from core.clients import ClientFactory
from core.errors import SITE_MISMATCH, classify_exception
from core.validation import validate_region_id
from costs.billing import default_client_builder as default_bss_builder
from costs.catalog import Flavor, Quote, VolumeType
from costs.flavor_names import lista_zonas, zonas_flavor
from costs.configuration import Component

PRODUCT_CODES: Dict[str, Tuple[str, str, Optional[int]]] = {
    # producto: (cloud_service_type, resource_type, size_measure_id)
    "ecs": ("hws.service.type.ec2", "hws.resource.type.vm", None),
    "evs": ("hws.service.type.ebs", "hws.resource.type.volume", 17),
    "ip": ("hws.service.type.vpc", "hws.resource.type.ip", None),
    "bandwidth": ("hws.service.type.vpc", "hws.resource.type.bandwidth", 15),
    # Ancho de banda cobrado por TRÁFICO (spec 12_<tipo>): precio por GB; la plantilla oficial
    # NO envía tamaño (el precio por GB no depende del límite en Mbps).
    "traffic": ("hws.service.type.vpc", "hws.resource.type.bandwidth", None),
    # OBS: la plantilla oficial cotiza la CLASE de almacenamiento (obs.standard/warm/cold) por
    # hora (usage_factor Duration), sin tamaño. Solo pago por uso.
    "obs_storage": ("hws.service.type.obs", "hws.resource.type.obs", None),
}
HOUR_MEASURE_ID = 4
MONTH_PERIOD_TYPE = 2
YEAR_PERIOD_TYPE = 3          # documentado en PeriodProductInfo.period_type (3 = año)
MONEY_MEASURE_ID = 1
# Unidad de uso "GB" para el tráfico (usage_factor "upflow"); confirmado en la plantilla
# oficial "eip-flow" de Huawei Cloud (usage_measure_id=10).
GB_MEASURE_ID = 10
PERIODS = {"monthly": (MONTH_PERIOD_TYPE, "month"), "yearly": (YEAR_PERIOD_TYPE, "year")}


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
    if component.product not in PRODUCT_CODES:
        raise PricingDataError(f"sin códigos BSS confirmados para {component.product}")
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
    models = bss_models()
    DemandProductInfo = models.DemandProductInfo
    ListOnDemandResourceRatingsRequest = models.ListOnDemandResourceRatingsRequest
    ListRateOnPeriodDetailRequest = models.ListRateOnPeriodDetailRequest
    PeriodProductInfo = models.PeriodProductInfo
    RateOnDemandReq = models.RateOnDemandReq
    RateOnPeriodReq = models.RateOnPeriodReq

    now = now or datetime.now(timezone.utc)
    product = _product_kwargs(component, region)
    if component.product == "traffic":
        # Tráfico: siempre por uso, precio de 1 GB de salida.
        response = client.list_on_demand_resource_ratings(ListOnDemandResourceRatingsRequest(body=RateOnDemandReq(
            project_id=project_id, inquiry_precision=1, product_infos=[DemandProductInfo(
                usage_factor="upflow", usage_value=1, usage_measure_id=GB_MEASURE_ID, **product)])))
        amount, measure = getattr(response, "official_website_amount", None), getattr(response, "measure_id", None)
        period, detail, billing_mode = "gb", "ListOnDemandResourceRatings (upflow, 1 GB)", "on_demand"
    elif billing_mode == "on_demand":
        response = client.list_on_demand_resource_ratings(ListOnDemandResourceRatingsRequest(body=RateOnDemandReq(
            project_id=project_id, inquiry_precision=1, product_infos=[DemandProductInfo(
                usage_factor="Duration", usage_value=1, usage_measure_id=HOUR_MEASURE_ID, **product)])))
        amount, measure = getattr(response, "official_website_amount", None), getattr(response, "measure_id", None)
        period, detail = "hour", "ListOnDemandResourceRatings"
    else:
        if billing_mode not in PERIODS:
            raise PricingDataError(f"modo de cobro no soportado: {billing_mode}")
        period_type, period = PERIODS[billing_mode]
        response = client.list_rate_on_period_detail(ListRateOnPeriodDetailRequest(body=RateOnPeriodReq(
            project_id=project_id, product_infos=[PeriodProductInfo(
                period_type=period_type, period_num=1, **product)])))
        result = getattr(response, "official_website_rating_result", None)
        amount, measure = getattr(result, "official_website_amount", None), getattr(result, "measure_id", None)
        detail = "ListRateOnPeriodDetail" + (" (1 año)" if period == "year" else "")
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
            reason = error.safe_explanation if error.kind == SITE_MISMATCH else error.message
            failures.append({"component": component.label, "region": region, "reason": reason or error.message})
    return quotes, failures


def default_ecs_builder(clients: ClientFactory, region: str, project_id: str) -> Any:
    from huaweicloudsdkecs.v2 import EcsClient
    from huaweicloudsdkecs.v2.region.ecs_region import EcsRegion

    return clients.create(EcsClient, EcsRegion, "ecs", region, project_id)


def _texto(valor: Any, largo: int) -> Optional[str]:
    texto = str(valor).strip() if valor not in (None, "") else ""
    return texto[:largo] or None


def _entero(valor: Any) -> Optional[int]:
    try:
        return int(str(valor).strip())
    except (TypeError, ValueError):
        return None


def _gbps(valor: Any) -> Optional[Decimal]:
    """``quota:max_rate`` viene en Mbit/s (p. ej. "8000"); se guarda en Gbit/s."""
    mbps = _entero(valor)
    return (Decimal(mbps) / Decimal(1000)).quantize(Decimal("0.01")) if mbps is not None and mbps > 0 else None


def fetch_flavors(clients: ClientFactory, *, region: str, project_id: str,
                  client_builder: Callable[[ClientFactory, str, str], Any] = default_ecs_builder) -> List[Flavor]:
    """Flavors ECS de la región con su estado oficial (``normal``/``sellout``/``obt``…) y sus
    zonas. Solo se excluyen los retirados (``abandon``), que la consola tampoco muestra.
    Lanza las excepciones del SDK."""
    from huaweicloudsdkecs.v2 import ListFlavorsRequest

    region = validate_region_id(region)
    response = client_builder(clients, region, project_id).list_flavors(ListFlavorsRequest())
    flavors: List[Flavor] = []
    for item in getattr(response, "flavors", None) or []:
        specs = getattr(item, "os_extra_specs", None)
        status = (_texto(getattr(specs, "condoperationstatus", None), 16) or "normal").lower()
        if status == "abandon":
            continue
        try:
            vcpus, ram = int(str(getattr(item, "vcpus", ""))), int(getattr(item, "ram", 0))
        except (TypeError, ValueError):
            continue
        flavor_id = str(getattr(item, "id", "") or getattr(item, "name", ""))[:64]
        if flavor_id:
            flavors.append(Flavor(flavor_id=flavor_id, vcpus=vcpus, ram_mb=ram,
                                  performance_type=getattr(specs, "ecsperformancetype", None),
                                  generation=getattr(specs, "ecsgeneration", None),
                                  status=status,
                                  az_status=zonas_flavor(getattr(specs, "condoperationaz", None)) or None,
                                  architecture=_texto(getattr(specs, "ecsinstance_architecture", None), 16),
                                  cpu_name=_texto(getattr(specs, "infocpuname", None), 128),
                                  gpu_name=_texto(getattr(specs, "infogpuname", None), 128),
                                  max_bandwidth_gbps=_gbps(getattr(specs, "quotamax_rate", None)),
                                  max_pps=_entero(getattr(specs, "quotamax_pps", None))))
    return flavors


def default_evs_builder(clients: ClientFactory, region: str, project_id: str) -> Any:
    from huaweicloudsdkevs.v2 import EvsClient
    from huaweicloudsdkevs.v2.region.evs_region import EvsRegion

    return clients.create(EvsClient, EvsRegion, "evs", region, project_id)


def fetch_volume_types(clients: ClientFactory, *, region: str, project_id: str,
                       client_builder: Callable[[ClientFactory, str, str], Any] = default_evs_builder
                       ) -> List[VolumeType]:
    """Tipos de disco EVS de la región (EVS ``CinderListVolumeTypes``, ``GET /v2/{project_id}/types``):
    zonas que lo ofrecen (``RESKEY:availability_zones``) y zonas agotadas
    (``os-vendor-extended:sold_out_availability_zones``). Lanza las excepciones del SDK."""
    from huaweicloudsdkevs.v2 import CinderListVolumeTypesRequest

    region = validate_region_id(region)
    response = client_builder(clients, region, project_id).cinder_list_volume_types(CinderListVolumeTypesRequest())
    types: List[VolumeType] = []
    for item in getattr(response, "volume_types", None) or []:
        name = _texto(getattr(item, "name", None), 64)
        if not name:
            continue
        specs = getattr(item, "extra_specs", None)
        zonas = lista_zonas(getattr(specs, "reske_yavailability_zones", None))
        agotadas = lista_zonas(getattr(specs, "os_vendor_extendedsold_out_availability_zones", None))
        types.append(VolumeType(name=name, availability_zones=tuple(zonas), sold_out_zones=tuple(agotadas)))
    return types


# ---------------------------------------------------------------- códigos BSS (oficiales)
def _paged(call: Callable[[int, int], Any], attribute: str, limit: int = 100, max_pages: int = 50) -> List[Any]:
    items: List[Any] = []
    for page in range(max_pages):
        response = call(page * limit, limit)
        chunk = list(getattr(response, attribute, None) or [])
        items += chunk
        total = getattr(response, "total_count", None)
        if not chunk or len(chunk) < limit or (total is not None and len(items) >= int(total)):
            break
    return items


def fetch_bss_codes(clients: ClientFactory, *,
                    client_builder: Callable[[ClientFactory], Any] = default_bss_builder) -> List[Dict[str, Any]]:
    """Códigos de servicio y de recurso de BSS (``ListServiceTypes`` + ``ListResourceTypes``)."""
    models = bss_models()
    client = client_builder(clients)
    services = _paged(lambda offset, limit: client.list_service_types(
        models.ListServiceTypesRequest(offset=offset, limit=limit)), "service_types")
    resources = _paged(lambda offset, limit: client.list_resource_types(
        models.ListResourceTypesRequest(offset=offset, limit=limit)), "resource_types")
    codes = [{"kind": "service", "code": getattr(s, "service_type_code", None),
              "name": " · ".join(x for x in (getattr(s, "abbreviation", None), getattr(s, "service_type_name", None)) if x),
              "parent_code": None} for s in services]
    codes += [{"kind": "resource", "code": getattr(r, "resource_type_code", None),
               "name": getattr(r, "resource_type_name", None), "parent_code": getattr(r, "service_type_code", None)}
              for r in resources]
    return [c for c in codes if c["code"]]


def fetch_usage_types(clients: ClientFactory, resource_type_code: str, *,
                      client_builder: Callable[[ClientFactory], Any] = default_bss_builder) -> List[Dict[str, Any]]:
    """Tipos de uso de un recurso (``ListUsageTypes``), p. ej. capacidad de almacenamiento de OBS."""
    models = bss_models()
    client = client_builder(clients)
    usages = _paged(lambda offset, limit: client.list_usage_types(models.ListUsageTypesRequest(
        resource_type_code=resource_type_code, offset=offset, limit=limit)), "usage_types")
    return [{"kind": "usage", "code": getattr(u, "code", None), "name": getattr(u, "name", None),
             "parent_code": resource_type_code} for u in usages if getattr(u, "code", None)]


# ---------------------------------------------------------------- RDS (oficial)
RDS_ENGINES = ("MySQL", "PostgreSQL", "SQLServer")


def default_rds_builder(clients: ClientFactory, region: str, project_id: str) -> Any:
    from huaweicloudsdkrds.v3 import RdsClient
    from huaweicloudsdkrds.v3.region.rds_region import RdsRegion

    return clients.create(RdsClient, RdsRegion, "rds", region, project_id)


def fetch_rds_versions(clients: ClientFactory, *, region: str, project_id: str, engine: str,
                       client_builder: Callable[[ClientFactory, str, str], Any] = default_rds_builder) -> List[str]:
    """Versiones del motor en la región (RDS ``ListDatastores``)."""
    from huaweicloudsdkrds.v3 import ListDatastoresRequest

    if engine not in RDS_ENGINES:
        raise ValueError("Motor RDS no soportado.")
    response = client_builder(clients, validate_region_id(region), project_id).list_datastores(
        ListDatastoresRequest(database_name=engine))
    return [str(d.name) for d in (getattr(response, "data_stores", None) or []) if getattr(d, "name", None)]


def fetch_rds_flavors(clients: ClientFactory, *, region: str, project_id: str, engine: str, version: str,
                      client_builder: Callable[[ClientFactory, str, str], Any] = default_rds_builder) -> List[Dict[str, Any]]:
    """Flavors de RDS del motor/versión (RDS ``ListFlavors``) con su estado por zona."""
    from huaweicloudsdkrds.v3 import ListFlavorsRequest

    if engine not in RDS_ENGINES:
        raise ValueError("Motor RDS no soportado.")
    response = client_builder(clients, validate_region_id(region), project_id).list_flavors(
        ListFlavorsRequest(database_name=engine, version_name=version))
    flavors = []
    for f in getattr(response, "flavors", None) or []:
        try:
            vcpus, ram = int(str(getattr(f, "vcpus", ""))), Decimal(str(getattr(f, "ram", "")))
        except (TypeError, ValueError, InvalidOperation):
            continue
        spec = _texto(getattr(f, "spec_code", None), 100)
        mode = (_texto(getattr(f, "instance_mode", None), 16) or "").lower()
        if spec and mode:
            flavors.append({"spec_code": spec, "vcpus": vcpus, "ram_gb": ram, "instance_mode": mode,
                            "az_status": dict(getattr(f, "az_status", None) or {}) or None})
    return flavors
