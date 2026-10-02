# coding: utf-8
"""Construcción de clientes del SDK de Huawei Cloud.

Centraliza credenciales, resolución de región/endpoint, configuración HTTP y la
protección de llamadas (``core.throttling``: límite global y reintentos ante 429)
para que los collectors no repitan el patrón ``new_builder()...build()``.
Los clientes se crean por tarea: nunca se comparten entre hilos.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

from huaweicloudsdkcore.auth.credentials import BasicCredentials
from huaweicloudsdkcore.http.http_config import HttpConfig
from huaweicloudsdkcore.region.region import Region

from core.credentials import HuaweiCredentials
from core.throttling import GLOBAL_CALL_GATE, CallGate, GuardedClient, RetryCounter, RetryPolicy

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT_DOMAIN = "myhuaweicloud.com"


class ClientFactory:
    """Crea clientes del SDK para una cuenta (credenciales) dada."""

    def __init__(
        self,
        credentials: HuaweiCredentials,
        *,
        endpoint_domain: str = DEFAULT_ENDPOINT_DOMAIN,
        http_config: Optional[HttpConfig] = None,
        retry_policy: Optional[RetryPolicy] = None,
        gate: Optional[CallGate] = None,
    ) -> None:
        self._credentials = credentials
        self._endpoint_domain = endpoint_domain
        self._http_config = http_config
        self._retry_policy = retry_policy or RetryPolicy()
        self._gate = gate or GLOBAL_CALL_GATE
        self.retries = RetryCounter()

    @property
    def secrets(self) -> Tuple[str, str]:
        return self._credentials.secrets

    def resolve_region(self, region_id: str, region_cls: Any, endpoint_prefix: str) -> Region:
        """Usa la región declarada en el SDK; si no existe, construye el endpoint estándar."""
        try:
            return region_cls.value_of(region_id)
        except KeyError:
            endpoint = f"https://{endpoint_prefix}.{region_id}.{self._endpoint_domain}"
            logger.info("Región %s no declarada en %s; se usa %s",
                        region_id, getattr(region_cls, "__name__", region_cls), endpoint)
            return Region(id=region_id, endpoint=endpoint)

    def create(self, client_cls: Any, region_cls: Any, endpoint_prefix: str,
               region_id: str, project_id: str) -> Any:
        """Cliente regional autenticado con AK/SK + Project ID."""
        credentials = BasicCredentials(
            ak=self._credentials.ak, sk=self._credentials.sk, project_id=project_id
        )
        return self._build(client_cls, credentials,
                           self.resolve_region(region_id, region_cls, endpoint_prefix))

    def create_global(self, client_cls: Any, region_cls: Any, endpoint_prefix: str,
                      region_id: str, domain_id: Optional[str] = None) -> Any:
        """Cliente de servicio global (IAM) con ``GlobalCredentials``.

        Sin ``domain_id`` el SDK lo obtiene automáticamente con una llamada a IAM.
        """
        from huaweicloudsdkcore.auth.credentials import GlobalCredentials

        credentials = GlobalCredentials(ak=self._credentials.ak, sk=self._credentials.sk,
                                        domain_id=domain_id or None)
        return self._build(client_cls, credentials,
                           self.resolve_region(region_id, region_cls, endpoint_prefix))

    def create_obs(self, region_id: str) -> Any:
        """Cliente OBS (firma propia de OBS, sin Project ID)."""
        from huaweicloudsdkobs.v1.obs_client import ObsClient
        from huaweicloudsdkobs.v1.obs_credentials import ObsCredentials
        from huaweicloudsdkobs.v1.region.obs_region import ObsRegion

        credentials = ObsCredentials(ak=self._credentials.ak, sk=self._credentials.sk)
        return self._build(ObsClient, credentials, self.resolve_region(region_id, ObsRegion, "obs"))

    def _build(self, client_cls: Any, credentials: Any, region: Region) -> Any:
        builder = client_cls.new_builder().with_credentials(credentials).with_region(region)
        if self._http_config is not None:
            builder = builder.with_http_config(self._http_config)
        return self.guard(builder.build())

    def guard(self, client: Any) -> GuardedClient:
        """Envuelve un cliente: límite global de llamadas y reintentos ante throttling (429)."""
        return GuardedClient(client, policy=self._retry_policy, gate=self._gate, counter=self.retries)
