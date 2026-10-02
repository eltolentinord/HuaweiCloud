# coding: utf-8
"""OBS — Object Storage Service.

``ListBuckets`` es una API de ámbito de CUENTA: desde cualquier endpoint regional
devuelve los buckets de TODAS las regiones, cada uno con su ``location``. No
admite paginación. La respuesta anida la lista en ``buckets.bucket`` (antes se
iteraba el contenedor ``Buckets`` directamente, lo que fallaba).

Por eso el collector es ``scope = "global"`` y asigna a cada recurso la región
real del bucket (``location``). Filtrar por la región consultada es tarea de
quien presenta los datos. Un bucket sin ``location`` queda con región vacía.
"""

from __future__ import annotations

from typing import Any, Dict

from collectors.base import BaseCollector, CollectorContext
from core.models import CollectionResult, Resource
from core.pagination import SinglePage
from core.serialization import first_present


class ObsCollector(BaseCollector):
    service = "obs"
    scope = "global"

    def collect(self, ctx: CollectorContext) -> CollectionResult:
        from huaweicloudsdkobs.v1 import ListBucketsRequest

        client = ctx.clients.create_obs(ctx.region)
        try:
            buckets = self.list_all(client.list_buckets, ListBucketsRequest, SinglePage(),
                                    items=self.bucket_list, id_key="name")
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # el cierre nunca debe ocultar el resultado
                    pass
        return CollectionResult(resources=[self.normalize(ctx, b) for b in buckets])

    @staticmethod
    def bucket_list(response: Any) -> Any:
        container = getattr(response, "buckets", None)
        if isinstance(container, (list, tuple)) or container is None:
            return container
        if isinstance(container, dict):
            return container.get("bucket") or container.get("Bucket")
        return getattr(container, "bucket", None)

    def normalize(self, ctx: CollectorContext, raw: Dict[str, Any]) -> Resource:
        name = first_present(raw, "name", "Name")
        location = first_present(raw, "location", "Location", "bucket_location", default="")
        return self.resource(
            ctx,
            raw,
            resource_type="obs.bucket",
            provider_id=name,
            name=name,
            status="",
            region=str(location),
            created_at=first_present(raw, "creation_date", "CreationDate"),
            attributes={
                "location": location or None,
                "cluster_type": raw.get("cluster_type"),
                "bucket_type": raw.get("bucket_type"),
            },
        )
