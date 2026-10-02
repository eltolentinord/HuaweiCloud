# coding: utf-8
"""Estrategias de paginación reutilizables.

Cada API de Huawei Cloud pagina de forma distinta (confirmado en los docstrings
del SDK 3.1.216):

- ``PagePagination``   ECS (``offset`` = número de página, desde 1), WAF (``page``).
- ``OffsetPagination`` EVS, CBR, RDS, DCS, CES, HSS, CFW (``offset`` = nº de registros).
- ``MarkerPagination`` EIP v2, NAT (``marker`` = ID del último elemento).
- ``TokenPagination``  VPC v3, ELB v3, VPN v5 (``page_info.next_marker`` devuelto por la API).
- ``SinglePage``       OBS ListBuckets, VPN ListVgws (sin paginación en la API).

``paginate`` protege contra bucles infinitos, cursores repetidos, páginas
vacías, respuestas inválidas y duplicados.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Hashable, List, Optional

from core.errors import PaginationError

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 100
# Salvaguarda contra bucles: 10 000 páginas (1 000 000 de recursos con 100/página).
DEFAULT_MAX_PAGES = 10_000

Params = Dict[str, Any]


@dataclass
class Page:
    """Una página ya serializada."""

    items: List[Dict[str, Any]] = field(default_factory=list)
    total: Optional[int] = None
    next_token: Optional[str] = None


class PaginationStrategy(ABC):
    """Decide los parámetros de la primera página y de las siguientes."""

    @abstractmethod
    def first(self) -> Params:
        ...

    @abstractmethod
    def next(self, params: Params, page: Page, collected: int) -> Optional[Params]:
        """Parámetros de la siguiente página o ``None`` para terminar."""

    def cursor(self, params: Params) -> Hashable:
        """Identifica la posición pedida; se usa para detectar cursores repetidos."""
        return tuple(sorted((k, str(v)) for k, v in params.items()))


class SinglePage(PaginationStrategy):
    def first(self) -> Params:
        return {}

    def next(self, params: Params, page: Page, collected: int) -> Optional[Params]:
        return None


@dataclass
class PagePagination(PaginationStrategy):
    """Número de página (base 1) + tamaño de página."""

    limit: int = DEFAULT_PAGE_SIZE
    page_param: str = "page"
    size_param: str = "limit"
    start: int = 1

    def first(self) -> Params:
        return {self.page_param: self.start, self.size_param: self.limit}

    def next(self, params: Params, page: Page, collected: int) -> Optional[Params]:
        if page.total is not None:
            if collected >= page.total:
                return None
        elif len(page.items) < self.limit:
            return None
        return {**params, self.page_param: params[self.page_param] + 1}


@dataclass
class OffsetPagination(PaginationStrategy):
    """Desplazamiento en número de registros + límite."""

    limit: int = DEFAULT_PAGE_SIZE
    offset_param: str = "offset"
    limit_param: str = "limit"
    start: int = 0

    def first(self) -> Params:
        return {self.offset_param: self.start, self.limit_param: self.limit}

    def next(self, params: Params, page: Page, collected: int) -> Optional[Params]:
        next_offset = params[self.offset_param] + len(page.items)
        if page.total is not None:
            # Con total conocido no dependemos de que la API respete ``limit``.
            if next_offset >= page.total:
                return None
        elif len(page.items) < self.limit:
            return None
        return {**params, self.offset_param: next_offset}


@dataclass
class MarkerPagination(PaginationStrategy):
    """``marker`` = ID del último elemento de la página anterior."""

    limit: int = DEFAULT_PAGE_SIZE
    marker_param: str = "marker"
    limit_param: str = "limit"
    id_key: str = "id"

    def first(self) -> Params:
        return {self.limit_param: self.limit}

    def next(self, params: Params, page: Page, collected: int) -> Optional[Params]:
        if len(page.items) < self.limit:
            return None
        marker = page.items[-1].get(self.id_key)
        if not marker:
            raise PaginationError(f"el último elemento no tiene '{self.id_key}' para usar como marker")
        return {**params, self.marker_param: marker}


@dataclass
class TokenPagination(PaginationStrategy):
    """Token/marker opaco devuelto por la API (p. ej. ``page_info.next_marker``)."""

    limit: int = DEFAULT_PAGE_SIZE
    token_param: str = "marker"
    limit_param: str = "limit"

    def first(self) -> Params:
        return {self.limit_param: self.limit}

    def next(self, params: Params, page: Page, collected: int) -> Optional[Params]:
        if not page.next_token:
            return None
        if page.total is not None and collected >= page.total:
            return None
        return {**params, self.token_param: page.next_token}


def paginate(
    fetch: Callable[[Params], Page],
    strategy: PaginationStrategy,
    *,
    id_key: Optional[str] = "id",
    max_pages: int = DEFAULT_MAX_PAGES,
    label: str = "",
) -> List[Dict[str, Any]]:
    """Recorre todas las páginas y devuelve los elementos sin duplicados.

    Lanza ``PaginationError`` si la API repite un cursor, devuelve una página
    completa ya vista (señal de que ignora la paginación), devuelve un formato
    inválido o se supera ``max_pages``. Nunca devuelve datos truncados en silencio.
    """
    params: Params = strategy.first()
    seen_cursors = set()
    seen_ids = set()
    items: List[Dict[str, Any]] = []

    for page_number in range(1, max_pages + 1):
        cursor = strategy.cursor(params)
        if cursor in seen_cursors:
            raise PaginationError(f"{label}: cursor repetido en la página {page_number}")
        seen_cursors.add(cursor)

        page = fetch(params)
        if not isinstance(page, Page) or not isinstance(page.items, list):
            raise PaginationError(f"{label}: respuesta de página inválida")
        if not page.items:
            break

        new_items = 0
        for item in page.items:
            if not isinstance(item, dict):
                raise PaginationError(f"{label}: elemento no es un objeto")
            item_id = item.get(id_key) if id_key else None
            if item_id is not None:
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
            items.append(item)
            new_items += 1

        if new_items == 0:
            raise PaginationError(
                f"{label}: la página {page_number} solo contiene elementos repetidos"
            )
        if new_items < len(page.items):
            logger.warning("%s: %d duplicados descartados en la página %d",
                           label, len(page.items) - new_items, page_number)

        next_params = strategy.next(params, page, len(items))
        if next_params is None:
            break
        params = next_params
    else:
        raise PaginationError(f"{label}: se superó el máximo de {max_pages} páginas")

    logger.debug("%s: %d elementos en %d página(s)", label, len(items), page_number)
    return items
