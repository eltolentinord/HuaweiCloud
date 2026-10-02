# coding: utf-8
"""Validación de valores que acaban formando URLs (prevención de SSRF).

``ClientFactory`` construye endpoints como ``https://{servicio}.{region}.{endpoint_domain}``
cuando la región no está declarada en el SDK. Sin validar, una cuenta con
``endpoint_domain = "atacante.example"`` o una región ``x.atacante.example/`` haría
que las peticiones FIRMADAS con la AK/SK salieran hacia un host arbitrario.

- Dominios permitidos: los de Huawei Cloud (``myhuaweicloud.com``, ``myhuaweicloud.eu``)
  más los que se añadan explícitamente en ``INVENTORY_ALLOWED_ENDPOINT_DOMAINS``.
- Regiones: formato ``xx-nombre-N`` (letras minúsculas, dígitos y guiones).
"""

from __future__ import annotations

import os
import re
from typing import FrozenSet, Optional

DEFAULT_ENDPOINT_DOMAINS = frozenset({"myhuaweicloud.com", "myhuaweicloud.eu"})
ENV_ALLOWED_ENDPOINT_DOMAINS = "INVENTORY_ALLOWED_ENDPOINT_DOMAINS"
REGION_ID_PATTERN = re.compile(r"^[a-z]{2,4}(?:-[a-z0-9]+){1,4}$")
_DOMAIN_PATTERN = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class InvalidValueError(ValueError):
    """Valor rechazado por seguridad o formato."""


def allowed_endpoint_domains() -> FrozenSet[str]:
    extra = os.environ.get(ENV_ALLOWED_ENDPOINT_DOMAINS, "")
    return DEFAULT_ENDPOINT_DOMAINS | {d.strip().lower() for d in extra.split(",") if d.strip()}


def validate_endpoint_domain(value: Optional[str]) -> str:
    domain = (value or "").strip().lower()
    if not _DOMAIN_PATTERN.match(domain) or domain not in allowed_endpoint_domains():
        raise InvalidValueError(
            "Dominio de endpoint no permitido. Usa uno de: " + ", ".join(sorted(allowed_endpoint_domains())))
    return domain


def validate_region_id(value: Optional[str]) -> str:
    region = (value or "").strip()
    if not REGION_ID_PATTERN.match(region):
        raise InvalidValueError(f"Región con formato inválido: debe ser como 'ap-southeast-3'.")
    return region
