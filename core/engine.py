# coding: utf-8
"""Orquestación de collectors: ejecuta, mide y clasifica errores por servicio."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from collectors import get_collector
from collectors.base import BaseCollector, CollectorContext
from core.clients import DEFAULT_ENDPOINT_DOMAIN, ClientFactory
from core.credentials import CredentialProvider
from core.errors import AUTHENTICATION, ServiceError, classify_exception, mask_project_id_ascii, notice
from core.models import Resource
from core.observability import METRICS

logger = logging.getLogger(__name__)


@dataclass
class ServiceRun:
    """Resultado de un servicio en un contexto (región + proyecto)."""

    service: str
    scope: str
    region: str
    resources: List[Resource] = field(default_factory=list)
    notices: List[ServiceError] = field(default_factory=list)
    error: Optional[ServiceError] = None
    duration_ms: int = 0
    complete: bool = True  # False = cobertura parcial (no permite inferir eliminaciones)

    @property
    def ok(self) -> bool:
        return self.error is None


def run_collector(collector: BaseCollector, ctx: CollectorContext) -> ServiceRun:
    """Ejecuta un collector sin propagar excepciones (se convierten en ``ServiceError``)."""
    started = time.perf_counter()
    run = ServiceRun(service=collector.service, scope=collector.scope, region=ctx.region)
    try:
        result = collector.collect(ctx)
        run.resources, run.notices, run.complete = result.resources, result.notices, result.complete
    except Exception as exc:  # cualquier fallo de un servicio no detiene al resto
        run.error = classify_exception(
            exc,
            service=collector.service,
            region=ctx.region,
            project_id=ctx.project_id,
            secrets=ctx.clients.secrets,
        )
        if run.error.kind == "internal":
            logger.exception("Fallo interno en %s", collector.service)
        METRICS.inc("collector_errors_total", help="Errores de collectors por categoría",
                    service=collector.service, kind=run.error.kind)
    run.duration_ms = int((time.perf_counter() - started) * 1000)
    METRICS.observe("collector_duration_seconds", run.duration_ms / 1000, help="Duración de collectors",
                    service=collector.service)
    if run.error:
        run.error.duration_ms = run.duration_ms
        logger.warning("Inventario %s", run.error.to_log())
    else:
        logger.info("Inventario service=%s region=%s project=%s recursos=%d duration_ms=%d",
                    collector.service, ctx.region, mask_project_id_ascii(ctx.project_id),
                    len(run.resources), run.duration_ms)
    return run


def collect_inventory(
    credential_provider: CredentialProvider,
    region: str,
    project_id: str,
    services: Iterable[str],
    *,
    client_factory: Optional[ClientFactory] = None,
    endpoint_domain: str = DEFAULT_ENDPOINT_DOMAIN,
) -> List[ServiceRun]:
    """Recolecta los servicios indicados (en orden) para una región y proyecto."""
    clients = client_factory or ClientFactory(credential_provider.get_credentials(),
                                              endpoint_domain=endpoint_domain)
    ctx = CollectorContext(clients=clients, region=region, project_id=project_id)
    runs: List[ServiceRun] = []
    services = list(services)
    for index, service in enumerate(services):
        run = run_collector(get_collector(service), ctx)
        runs.append(run)
        if run.error and run.error.kind == AUTHENTICATION and index + 1 < len(services):
            # Credenciales rechazadas por el gateway (APIGW.*): todos los servicios fallarían igual.
            skipped = services[index + 1:]
            run.notices.append(notice(
                service, f"Se omitieron {len(skipped)} servicio(s) porque las credenciales fueron "
                         f"rechazadas: {', '.join(s.upper() for s in skipped)}.", region=region))
            logger.warning("Credenciales rechazadas; se omiten %d servicios", len(skipped))
            break
    return runs
