# coding: utf-8
"""Contrato común de los collectors de servicios Huawei Cloud.

Un collector:
  1. obtiene un cliente del SDK a través de ``ClientFactory``;
  2. recorre todas las páginas con la estrategia que declara;
  3. normaliza cada elemento a ``Resource`` (sin etiquetas visuales).

No conoce HTML, Excel ni el formato de las tablas del frontend.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Dict, List, Optional, Tuple, Union

from huaweicloudsdkcore.exceptions import exceptions as sdk_exceptions

from core.clients import ClientFactory
from core.errors import PaginationError, ServiceError, notice
from core.models import CollectionResult, Resource
from core.pagination import Page, PaginationStrategy, paginate
from core.serialization import normalize_tags, redact_sensitive, serialize_items

logger = logging.getLogger(__name__)

ALL_GRANTED_EPS = "all_granted_eps"

Extractor = Union[str, Callable[[Any], Any]]


@dataclass(frozen=True)
class CollectorContext:
    """Ámbito de una recolección: una región y un Project ID de una cuenta."""

    clients: ClientFactory
    region: str
    project_id: str


def extract(response: Any, path: Optional[Extractor]) -> Any:
    """Lee ``a.b.c`` (atributos o claves) o aplica un callable sobre la respuesta."""
    if path is None:
        return None
    if callable(path):
        return path(response)
    value = response
    for part in path.split("."):
        if value is None:
            return None
        value = value.get(part) if isinstance(value, dict) else getattr(value, part, None)
    return value


def page_info_marker(response: Any) -> Optional[str]:
    """``page_info.next_marker`` (VPC v3, ELB v3, VPN v5)."""
    page_info = extract(response, "page_info")
    if page_info is None:
        return None
    if isinstance(page_info, dict):
        return page_info.get("next_marker")
    return getattr(page_info, "next_marker", None)


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class BaseCollector(ABC):
    """Clase base de los 15 collectors."""

    service: ClassVar[str]
    # "regional": recursos por región/proyecto. "global": la API devuelve recursos
    # de todas las regiones (p. ej. OBS) y debe consultarse una vez por cuenta.
    scope: ClassVar[str] = "regional"

    @abstractmethod
    def collect(self, ctx: CollectorContext) -> CollectionResult:
        """Devuelve todos los recursos del servicio en el contexto dado."""

    # ------------------------------------------------------------------ helpers
    def list_all(
        self,
        call: Callable[[Any], Any],
        make_request: Callable[..., Any],
        strategy: PaginationStrategy,
        *,
        items: Extractor,
        total: Optional[Extractor] = None,
        next_token: Optional[Callable[[Any], Optional[str]]] = None,
        id_key: Optional[str] = "id",
        label: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Pagina ``call(make_request(**params))`` y devuelve dicts serializados."""
        label = label or self.service

        def fetch(params: Dict[str, Any]) -> Page:
            response = call(make_request(**params))
            raw_items = extract(response, items)
            if raw_items is not None and not isinstance(raw_items, (list, tuple)):
                raise PaginationError(f"{label}: el campo de elementos no es una lista")
            return Page(
                items=serialize_items(raw_items),
                total=_to_int(extract(response, total)),
                next_token=next_token(response) if next_token else None,
            )

        return paginate(fetch, strategy, id_key=id_key, label=label)

    def with_all_enterprise_projects(
        self, ctx: CollectorContext, fetch: Callable[[Optional[str]], List[Dict[str, Any]]],
        notices: List[ServiceError],
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """Consulta todos los proyectos de empresa (``all_granted_eps``).

        Si la cuenta no tiene proyectos de empresa o el usuario no tiene permiso
        sobre EPS, la API responde 400/403; entonces se repite la consulta sin el
        parámetro (solo proyecto de empresa por defecto) y se emite un aviso.
        Devuelve ``(elementos, completo)``: el reintento es cobertura parcial.
        """
        try:
            return fetch(ALL_GRANTED_EPS), True
        except sdk_exceptions.ClientRequestException as exc:
            if exc.status_code not in (400, 403):
                raise
            logger.info("%s: all_granted_eps rechazado (HTTP %s); se reintenta sin EP",
                        self.service, exc.status_code)
        items = fetch(None)
        notices.append(notice(
            self.service,
            "No se pudieron consultar todos los proyectos de empresa (all_granted_eps); "
            "se muestran solo los recursos del proyecto de empresa por defecto.",
            region=ctx.region,
        ))
        return items, False

    def resource(
        self,
        ctx: CollectorContext,
        raw: Dict[str, Any],
        *,
        resource_type: str,
        provider_id: Any = None,
        name: Any = None,
        status: Any = None,
        created_at: Any = None,
        enterprise_project_id: Any = None,
        tags: Any = None,
        attributes: Optional[Dict[str, Any]] = None,
        region: Optional[str] = None,
    ) -> Resource:
        """Construye un ``Resource`` con valores por defecto comunes."""

        def opt_str(value: Any) -> Optional[str]:
            return None if value in (None, "") else str(value)

        return Resource(
            service=self.service,
            resource_type=resource_type,
            provider_id=opt_str(provider_id if provider_id is not None else raw.get("id")),
            name=opt_str(name if name is not None else raw.get("name")),
            status=opt_str(status if status is not None else raw.get("status")),
            region=ctx.region if region is None else region,
            project_id=ctx.project_id,
            created_at=opt_str(created_at if created_at is not None else raw.get("created_at")),
            enterprise_project_id=opt_str(
                enterprise_project_id if enterprise_project_id is not None
                else raw.get("enterprise_project_id")
            ),
            tags=normalize_tags(tags if tags is not None else raw.get("tags")),
            attributes=attributes or {},
            raw=redact_sensitive(raw),
        )


class SimpleListCollector(BaseCollector):
    """Collector de un solo tipo de recurso con un único ``list_*`` regional.

    Las subclases declaran el cliente, el método, la request, la estrategia y
    ``normalize``; la clase base hace el resto.
    """

    client_cls: ClassVar[Any]
    region_cls: ClassVar[Any]
    endpoint_prefix: ClassVar[str]
    method: ClassVar[str]
    request_cls: ClassVar[Any]
    items_field: ClassVar[Extractor]
    total_field: ClassVar[Optional[Extractor]] = None
    id_key: ClassVar[Optional[str]] = "id"
    resource_type: ClassVar[str]
    # True cuando la API filtra por defecto al proyecto de empresa "default" y
    # documenta ``all_granted_eps`` (HSS, WAF).
    all_enterprise_projects: ClassVar[bool] = False

    @abstractmethod
    def strategy(self) -> PaginationStrategy:
        ...

    @abstractmethod
    def normalize(self, ctx: CollectorContext, raw: Dict[str, Any]) -> Resource:
        ...

    def next_token(self, response: Any) -> Optional[str]:
        return None

    def client(self, ctx: CollectorContext) -> Any:
        return ctx.clients.create(self.client_cls, self.region_cls, self.endpoint_prefix,
                                  ctx.region, ctx.project_id)

    def fetch(self, client: Any, **filters: Any) -> List[Dict[str, Any]]:
        """Todas las páginas de ``method`` con filtros opcionales de la request."""
        request_cls = type(self).request_cls
        return self.list_all(
            getattr(client, self.method),
            lambda **params: request_cls(**filters, **params),
            self.strategy(),
            # type(self): un callable de clase accedido por instancia sería un método enlazado
            items=type(self).items_field,
            total=type(self).total_field,
            next_token=self.next_token,
            id_key=self.id_key,
        )

    def collect(self, ctx: CollectorContext) -> CollectionResult:
        client = self.client(ctx)
        notices: List[ServiceError] = []
        complete = True
        if self.all_enterprise_projects:
            raws, complete = self.with_all_enterprise_projects(
                ctx,
                lambda eps: self.fetch(client, **({"enterprise_project_id": eps} if eps else {})),
                notices,
            )
        else:
            raws = self.fetch(client)
        return CollectionResult(resources=[self.normalize(ctx, raw) for raw in raws], notices=notices,
                                complete=complete)
