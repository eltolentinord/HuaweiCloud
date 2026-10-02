# coding: utf-8
"""ECS — Elastic Cloud Server.

Paginación (SDK ``ListServersDetailsRequest``): ``offset`` es el NÚMERO DE PÁGINA
(base 1; 0 equivale a 1) y ``limit`` el tamaño (máx. 1000). La respuesta trae
``count`` con el total. Antes se sumaba ``limit`` al ``offset`` y la consulta se
detenía en 100 servidores.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from huaweicloudsdkecs.v2 import EcsClient, ListServersDetailsRequest
from huaweicloudsdkecs.v2.region.ecs_region import EcsRegion

from collectors.base import CollectorContext, SimpleListCollector
from core.models import Resource
from core.pagination import PagePagination
from core.serialization import as_dict, as_list, make_serializable, to_number

PAGE_SIZE = 100


def extract_addresses(raw: Dict[str, Any]) -> Tuple[List[str], List[str], List[str]]:
    """IPs privadas, públicas (floating) y MACs a partir de ``addresses``."""
    private: List[str] = []
    public: List[str] = []
    macs: List[str] = []
    for entries in as_dict(raw.get("addresses")).values():
        for entry in as_list(entries):
            entry = entry if isinstance(entry, dict) else make_serializable(entry)
            if not isinstance(entry, dict):
                continue
            address = entry.get("addr")
            if address:
                (public if entry.get("OS-EXT-IPS:type") == "floating" else private).append(address)
            mac = entry.get("OS-EXT-IPS-MAC:mac_addr")
            if mac:
                macs.append(mac)
    return private, public, macs


def operating_system(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Sistema operativo según la metadata estándar de ECS.

    ECS publica ``metadata.image_name`` (p. ej. "Ubuntu 22.04 server 64bit"),
    ``metadata.os_type`` y ``metadata.os_bit``. Si no existen se recurre al ID
    de la imagen, que era lo único que se mostraba antes.
    """
    metadata = as_dict(raw.get("metadata"))
    image_id = as_dict(raw.get("image")).get("id") or (raw.get("image") if isinstance(raw.get("image"), str) else None)
    return {
        "image_id": image_id,
        "image_name": metadata.get("image_name"),
        "os_type": metadata.get("os_type"),
        "os_bit": metadata.get("os_bit"),
        "os": metadata.get("image_name") or metadata.get("os_type") or image_id,
    }


class EcsCollector(SimpleListCollector):
    service = "ecs"
    resource_type = "ecs.server"
    client_cls = EcsClient
    region_cls = EcsRegion
    endpoint_prefix = "ecs"
    method = "list_servers_details"
    request_cls = ListServersDetailsRequest
    items_field = "servers"
    total_field = "count"

    def strategy(self) -> PagePagination:
        return PagePagination(limit=PAGE_SIZE, page_param="offset", size_param="limit", start=1)

    def normalize(self, ctx: CollectorContext, raw: Dict[str, Any]) -> Resource:
        flavor = as_dict(raw.get("flavor"))
        vcpus = to_number(flavor.get("vcpus"))
        ram_mb = to_number(flavor.get("ram"))
        private, public, macs = extract_addresses(raw)
        security_groups = [sg.get("name") for sg in as_list(raw.get("security_groups"))
                           if isinstance(sg, dict) and sg.get("name")]
        volumes = [v.get("id") for v in as_list(raw.get("os-extended-volumes:volumes_attached"))
                   if isinstance(v, dict) and v.get("id")]
        return self.resource(
            ctx,
            raw,
            resource_type=self.resource_type,
            created_at=raw.get("created"),
            attributes={
                "availability_zone": raw.get("OS-EXT-AZ:availability_zone"),
                "flavor_id": flavor.get("id"),
                "flavor_name": flavor.get("name") or (raw.get("flavor") if isinstance(raw.get("flavor"), str) else None),
                "vcpus": int(vcpus) if vcpus else 0,
                "ram_mb": int(ram_mb) if ram_mb else 0,
                "ram_gb": round(ram_mb / 1024, 1) if ram_mb else 0,
                "private_ips": private,
                "public_ips": public,
                "mac_addresses": macs,
                "networks": list(as_dict(raw.get("addresses")).keys()),
                "security_groups": security_groups,
                "volume_ids": volumes,
                "key_name": raw.get("key_name"),
                "tenant_id": raw.get("tenant_id"),
                "user_id": raw.get("user_id"),
                "updated_at": raw.get("updated"),
                "power_state": raw.get("OS-EXT-STS:power_state"),
                "vm_state": raw.get("OS-EXT-STS:vm_state"),
                "task_state": raw.get("OS-EXT-STS:task_state"),
                "metadata": as_dict(raw.get("metadata")),
                **operating_system(raw),
            },
        )
