# coding: utf-8
"""Cliente BSS (Billing Center) según el sitio de Huawei Cloud de la cuenta.

BSS es un servicio global, pero cada sitio de Huawei Cloud tiene su propio endpoint
y una cuenta solo puede consultar el de su sitio. Llamar al de otro sitio devuelve
HTTP 403 ``CBC.0156`` "Access denied. The customer does not belong to the website
you are now at" (tabla de errores de la API de Customer Operation Capabilities).

Sitios (confirmado en los SDK oficiales 3.1.216):

=============  ===========================  ======================  ==============
sitio          endpoint                     paquete del SDK         región del SDK
=============  ===========================  ======================  ==============
international  bss-intl.myhuaweicloud.com   huaweicloudsdkbssintl   ap-southeast-1
europe         bss.myhuaweicloud.eu         huaweicloudsdkbssintl   eu-west-101
china          bss.myhuaweicloud.com        huaweicloudsdkbss       cn-north-1
=============  ===========================  ======================  ==============

Ambos paquetes exponen las mismas operaciones, rutas y modelos (``ListCosts``,
``ListCustomerselfResourceRecords``, ``ListOnDemandResourceRatings``,
``ListRateOnPeriodDetail``); los modelos de petición se importan siempre del mismo
paquete que el cliente (``bss_models``).

Por ahora todas las cuentas usan ``DEFAULT_BSS_SITE`` (International). Para soportar
China o Europa basta con pasar otro ``site``; no hay que volver a fijar regiones en
los módulos de costos.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Dict, Optional

from core.clients import ClientFactory
from core.errors import SITE_MISMATCH_CODE, SITE_MISMATCH_MESSAGE  # noqa: F401  (reexportados)

INTERNATIONAL = "international"
EUROPE = "europe"
CHINA = "china"


@dataclass(frozen=True)
class BssSite:
    name: str
    package: str          # paquete del SDK oficial
    client_class: str     # clase cliente dentro de ``<package>.v2``
    region_module: str    # módulo que declara las regiones del SDK
    region_class: str
    region_id: str
    endpoint: str         # informativo/tests; el SDK lo resuelve desde ``region_id``


BSS_SITES: Dict[str, BssSite] = {
    INTERNATIONAL: BssSite(INTERNATIONAL, "huaweicloudsdkbssintl", "BssintlClient",
                           "huaweicloudsdkbssintl.v2.region.bssintl_region", "BssintlRegion",
                           "ap-southeast-1", "https://bss-intl.myhuaweicloud.com"),
    EUROPE: BssSite(EUROPE, "huaweicloudsdkbssintl", "BssintlClient",
                    "huaweicloudsdkbssintl.v2.region.bssintl_region", "BssintlRegion",
                    "eu-west-101", "https://bss.myhuaweicloud.eu"),
    CHINA: BssSite(CHINA, "huaweicloudsdkbss", "BssClient",
                   "huaweicloudsdkbss.v2.region.bss_region", "BssRegion",
                   "cn-north-1", "https://bss.myhuaweicloud.com"),
}
DEFAULT_BSS_SITE = INTERNATIONAL



def get_bss_site(site: Optional[str] = None) -> BssSite:
    name = (site or DEFAULT_BSS_SITE).strip().lower()
    try:
        return BSS_SITES[name]
    except KeyError:
        raise ValueError(f"Sitio BSS no soportado: {name!r}") from None


def bss_models(site: Optional[str] = None) -> ModuleType:
    """Módulo ``<paquete>.v2`` del sitio (clases de petición y modelos)."""
    return importlib.import_module(f"{get_bss_site(site).package}.v2")


def _client_and_region(site: BssSite) -> tuple:
    client_cls = getattr(bss_models(site.name), site.client_class)
    region_cls = getattr(importlib.import_module(site.region_module), site.region_class)
    return client_cls, region_cls


def create_bss_client(clients: ClientFactory, site: Optional[str] = None) -> Any:
    """Cliente BSS autenticado con las credenciales de la cuenta (``ClientFactory``)."""
    bss_site = get_bss_site(site)
    client_cls, region_cls = _client_and_region(bss_site)
    return clients.create_global(client_cls, region_cls, "bss", bss_site.region_id)


def create_bss_client_with_keys(ak: str, sk: str, site: Optional[str] = None) -> Any:
    """Cliente BSS para AK/SK que solo viven en memoria (página heredada ``/costos``)."""
    from huaweicloudsdkcore.auth.credentials import GlobalCredentials

    bss_site = get_bss_site(site)
    client_cls, region_cls = _client_and_region(bss_site)
    return (client_cls.new_builder()
            .with_credentials(GlobalCredentials(ak=ak, sk=sk))
            .with_region(region_cls.value_of(bss_site.region_id))
            .build())
