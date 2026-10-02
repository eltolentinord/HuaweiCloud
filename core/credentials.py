# coding: utf-8
"""Proveedores de credenciales.

Hoy las credenciales llegan del formulario (``StaticCredentialProvider``) o de
variables de entorno (``EnvCredentialProvider``). En la Fase 2 se añadirá un
proveedor respaldado por base de datos con secretos cifrados que implemente el
mismo protocolo ``CredentialProvider``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol, Tuple

ENV_AK = "HUAWEI_AK"
ENV_SK = "HUAWEI_SK"
ENV_PROJECT_ID = "HUAWEI_PROJECT_ID"
ENV_REGION = "HUAWEI_REGION"
DEFAULT_REGION = "ap-southeast-3"


class MissingCredentialsError(ValueError):
    """Faltan datos obligatorios de credenciales."""


@dataclass(frozen=True)
class HuaweiCredentials:
    """AK/SK de una cuenta. ``repr`` nunca muestra los secretos."""

    ak: str = field(repr=False)
    sk: str = field(repr=False)

    def __post_init__(self) -> None:
        if not (self.ak or "").strip() or not (self.sk or "").strip():
            raise MissingCredentialsError("AK y SK son obligatorios.")
        object.__setattr__(self, "ak", self.ak.strip())
        object.__setattr__(self, "sk", self.sk.strip())

    @property
    def secrets(self) -> Tuple[str, str]:
        """Valores que deben redactarse en cualquier mensaje o log."""
        return (self.ak, self.sk)

    def __repr__(self) -> str:
        return f"HuaweiCredentials(ak='{self.ak[:4]}…')"


class CredentialProvider(Protocol):
    def get_credentials(self) -> HuaweiCredentials:
        ...


@dataclass(frozen=True)
class StaticCredentialProvider:
    """Credenciales en memoria (p. ej. las enviadas por el formulario)."""

    credentials: HuaweiCredentials

    def get_credentials(self) -> HuaweiCredentials:
        return self.credentials


class EnvCredentialProvider:
    """Lee ``HUAWEI_AK`` / ``HUAWEI_SK`` del entorno (o de un mapeo de respaldo)."""

    def __init__(self, environ: Optional[Mapping[str, str]] = None,
                 fallback: Optional[Mapping[str, str]] = None) -> None:
        self._environ = os.environ if environ is None else environ
        self._fallback = fallback or {}

    def value(self, name: str, default: Optional[str] = None) -> Optional[str]:
        for source in (self._environ, self._fallback):
            raw = source.get(name)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        return default

    def get_credentials(self) -> HuaweiCredentials:
        return HuaweiCredentials(ak=self.value(ENV_AK) or "", sk=self.value(ENV_SK) or "")
