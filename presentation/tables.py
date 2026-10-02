# coding: utf-8
"""Recursos normalizados → tablas, detalle y KPIs del frontend actual.

Aquí (y solo aquí) viven las etiquetas visuales en español. El formato de salida
es el histórico de ``inventory.py``: ``{"titulo", "columnas", "filas"}`` donde
cada fila es ``{columna: valor, "_detalle": {...}}``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Sequence, Tuple

from core.models import Resource
from core.serialization import to_text

Getter = Callable[[Resource], Any]
Table = Dict[str, Any]
Summary = List[Dict[str, Any]]

RAW_DETAIL_LIMIT = 40


# --------------------------------------------------------------------- getters
def attr(key: str) -> Getter:
    return lambda r: to_text(r.attributes.get(key))


def number(key: str) -> Getter:
    return lambda r: r.attributes.get(key) or 0


def name(r: Resource) -> str:
    return to_text(r.name)


def status(r: Resource) -> str:
    return to_text(r.status)


def provider_id(r: Resource) -> str:
    return to_text(r.provider_id)


def region(r: Resource) -> str:
    return r.region


def created(r: Resource) -> str:
    return to_text(r.created_at)


def enterprise_project(r: Resource) -> str:
    return to_text(r.enterprise_project_id)


def tags_text(r: Resource) -> str:
    return to_text([f"{k}={v}" if v != "" else k for k, v in r.tags.items()])


def raw_detail(r: Resource) -> Dict[str, str]:
    """Primeras claves de la respuesta original (comportamiento histórico)."""
    return {str(k): to_text(v) for k, v in list(r.raw.items())[:RAW_DETAIL_LIMIT]}


def detail_from(*fields: Tuple[str, Getter]) -> Callable[[Resource], Dict[str, Any]]:
    return lambda r: {label: getter(r) for label, getter in fields}


# ----------------------------------------------------------------------- specs
@dataclass(frozen=True)
class TableSpec:
    resource_type: str
    title: str
    columns: Sequence[Tuple[str, Getter]]
    detail: Callable[[Resource], Dict[str, Any]] = raw_detail

    def build(self, resources: Sequence[Resource]) -> Table:
        rows = []
        for resource in resources:
            if resource.resource_type != self.resource_type:
                continue
            row = {label: getter(resource) for label, getter in self.columns}
            row["_detalle"] = self.detail(resource)
            rows.append(row)
        return {"titulo": self.title, "columnas": [c for c, _ in self.columns], "filas": rows}


ECS = TableSpec(
    "ecs.server",
    "ECS",
    [
        ("Nombre ECS", name),
        ("Estado", status),
        ("Región", region),
        ("Zona AZ", attr("availability_zone")),
        ("Flavor", attr("flavor_name")),
        ("vCPU", number("vcpus")),
        ("RAM (GB)", number("ram_gb")),
        ("IP privada", attr("private_ips")),
        ("IP pública", attr("public_ips")),
        ("Security Group", attr("security_groups")),
        ("Sistema operativo", attr("os")),
        ("Fecha de creación", created),
    ],
    detail_from(
        ("ECS ID", provider_id),
        ("Tenant ID", attr("tenant_id")),
        ("User ID", attr("user_id")),
        ("Enterprise Project ID", enterprise_project),
        ("Imagen", attr("image_id")),
        ("Sistema operativo", attr("os")),
        ("Tipo de SO", attr("os_type")),
        ("Flavor", attr("flavor_name")),
        ("Key pair", attr("key_name")),
        ("Redes", attr("networks")),
        ("Todas las IP", lambda r: to_text(r.attributes.get("private_ips", []) + r.attributes.get("public_ips", []))),
        ("MAC address", attr("mac_addresses")),
        ("Security Groups", attr("security_groups")),
        ("Volume IDs", attr("volume_ids")),
        ("Tags", tags_text),
        ("Metadata", attr("metadata")),
        ("Fecha de creación", created),
        ("Fecha de actualización", attr("updated_at")),
    ),
)

EVS = TableSpec(
    "evs.volume",
    "EVS - Volúmenes",
    [
        ("Nombre", name),
        ("Estado", status),
        ("Región", region),
        ("Zona AZ", attr("availability_zone")),
        ("Tamaño (GB)", number("size_gb")),
        ("Tipo", attr("volume_type")),
        ("Server ID", attr("server_id")),
        ("Fecha de creación", created),
    ],
    detail_from(
        ("ID", provider_id),
        ("Nombre", name),
        ("Estado", status),
        ("Zona AZ", attr("availability_zone")),
        ("Tamaño (GB)", attr("size_gb")),
        ("Tipo", attr("volume_type")),
        ("Server ID", attr("server_id")),
        ("Bootable", attr("bootable")),
        ("Cifrado", attr("encrypted")),
        ("Enterprise Project ID", enterprise_project),
        ("Tags", tags_text),
        ("Metadata", attr("metadata")),
        ("Fecha de creación", created),
        ("Fecha de actualización", attr("updated_at")),
    ),
)

VPC_VPCS = TableSpec(
    "vpc.vpc",
    "VPCs",
    [("Nombre", name), ("Estado", status), ("CIDR", attr("cidr")), ("Región", region),
     ("Fecha de creación", created)],
    detail_from(
        ("ID", provider_id), ("Nombre", name), ("Estado", status), ("CIDR", attr("cidr")),
        ("Descripción", attr("description")), ("Enterprise Project ID", enterprise_project),
    ),
)

VPC_SUBNETS = TableSpec(
    "vpc.subnet",
    "Subredes",
    [("Nombre", name), ("Estado", status), ("CIDR", attr("cidr")), ("Gateway IP", attr("gateway_ip")),
     ("Zona AZ", attr("availability_zone")), ("Fecha de creación", created)],
    detail_from(
        ("ID", provider_id), ("Nombre", name), ("Estado", status), ("CIDR", attr("cidr")),
        ("Gateway IP", attr("gateway_ip")), ("Zona AZ", attr("availability_zone")),
        ("VPC ID", attr("vpc_id")), ("DHCP", attr("dhcp_enabled")), ("Fecha de creación", created),
    ),
)

VPC_SECURITY_GROUPS = TableSpec(
    "vpc.security_group",
    "Security Groups",
    [("Nombre", name), ("ID", provider_id), ("Descripción", attr("description")), ("Región", region)],
    detail_from(
        ("ID", provider_id), ("Nombre", name), ("Descripción", attr("description")),
        ("Enterprise Project ID", enterprise_project), ("Creado", created),
    ),
)

VPN_GATEWAYS = TableSpec(
    "vpn.gateway",
    "VPN Gateways",
    [("Nombre", name), ("ID", provider_id), ("Estado", status),
     ("Nombre de conexión", attr("connection_ref")), ("Fecha de creación", created)],
)

VPN_CONNECTIONS = TableSpec(
    "vpn.connection",
    "Conexiones VPN",
    [("Nombre", name), ("ID", provider_id), ("Estado", status),
     ("Subredes locales", attr("local_subnets")), ("Subredes remotas", attr("remote_subnets")),
     ("Fecha de creación", created)],
)

OBS = TableSpec(
    "obs.bucket",
    "Buckets OBS",
    [("Nombre", name), ("Ubicación", attr("location")), ("Fecha de creación", created)],
)

EIP = TableSpec(
    "eip.publicip",
    "Elastic IPs",
    [("IP pública", attr("public_ip")), ("IP privada", attr("private_ip")), ("Estado", status),
     ("Puerto ID", attr("port_id")), ("ID", provider_id), ("Región", region), ("Creado", created)],
)

CBR = TableSpec(
    "cbr.vault",
    "CBR - Vaults",
    [("Nombre", name), ("ID", provider_id), ("Descripción", attr("description")),
     ("AZ", attr("availability_zone")), ("Región", region), ("Creado", created)],
)

ELB = TableSpec(
    "elb.loadbalancer",
    "ELB - Load Balancers",
    [("Nombre", name), ("Estado", status), ("IP VIP", attr("vip_address")), ("VPC ID", attr("vpc_id")),
     ("AZs", attr("availability_zones")), ("Región", region), ("Creado", created)],
)

RDS = TableSpec(
    "rds.instance",
    "RDS - Instancias",
    [("Nombre", name), ("Estado", status), ("Motor", attr("engine")), ("Versión", attr("engine_version")),
     ("Tipo", attr("instance_type")), ("Región", region), ("VPC ID", attr("vpc_id")), ("Creado", created)],
)

DCS = TableSpec(
    "dcs.instance",
    "DCS - Instancias",
    [("Nombre", name), ("Estado", status), ("Motor", attr("engine")), ("Versión", attr("engine_version")),
     ("Spec", attr("spec_code")), ("Puerto", attr("port")), ("IP", attr("ip")),
     ("AZs", attr("availability_zones")), ("Región", region)],
)

NAT = TableSpec(
    "nat.gateway",
    "NAT Gateways",
    [("Nombre", name), ("Estado", status), ("Spec", attr("spec")), ("Router ID", attr("router_id")),
     ("Red", attr("internal_network_id")), ("IP NAT", attr("ngport_ip_address")), ("Región", region),
     ("Creado", created)],
)

CES = TableSpec(
    "ces.alarm_rule",
    "Cloud Eye - Alarmas",
    [("Nombre", name), ("Alarm ID", provider_id), ("Habilitada", attr("enabled")),
     ("Namespace", attr("namespace")), ("Producto", attr("product_name")), ("Región", region)],
)

HSS = TableSpec(
    "hss.protected_server",
    "HSS - Servidores protegidos",
    [("Hostname", name), ("IP", attr("ip")), ("SO", attr("os")), ("Protección", attr("protect_status")),
     ("Estado host", attr("agent_status")), ("Agente", attr("agent_version")), ("Región", region)],
)

WAF = TableSpec(
    "waf.instance",
    "WAF - Instancias",
    [("Nombre", name), ("ID", provider_id), ("Estado", status), ("Run status", attr("run_status")),
     ("Acceso", attr("access_status")), ("Región", region), ("Zona", attr("zone")),
     ("Service IP", attr("service_ip")), ("VPC ID", attr("vpc_id"))],
)

CFW = TableSpec(
    "cfw.firewall",
    "CFW - Firewalls",
    [("Nombre", name), ("ID", provider_id), ("Estado", status), ("Tipo Servicio", attr("service_type")),
     ("Tipo HA", attr("ha_type")), ("Motor", attr("engine_type")), ("Flavor", attr("flavor")),
     ("Región", region)],
)

TABLE_SPECS: Dict[str, List[TableSpec]] = {
    "ecs": [ECS],
    "evs": [EVS],
    "vpc": [VPC_VPCS, VPC_SUBNETS, VPC_SECURITY_GROUPS],
    "vpn": [VPN_GATEWAYS, VPN_CONNECTIONS],
    "obs": [OBS],
    "eip": [EIP],
    "cbr": [CBR],
    "elb": [ELB],
    "rds": [RDS],
    "dcs": [DCS],
    "nat": [NAT],
    "ces": [CES],
    "hss": [HSS],
    "waf": [WAF],
    "cfw": [CFW],
}


# ------------------------------------------------------------------- resúmenes
def _count(label: str, resource_type: str) -> Callable[[Sequence[Resource]], Summary]:
    return lambda rs: [{"label": label, "value": sum(r.resource_type == resource_type for r in rs)}]


def _ecs_summary(resources: Sequence[Resource]) -> Summary:
    statuses = [(r.status or "").upper() for r in resources]
    return [
        {"label": "Total ECS", "value": len(resources)},
        {"label": "ECS activas", "value": statuses.count("ACTIVE")},
        {"label": "ECS apagadas", "value": statuses.count("SHUTOFF")},
        {"label": "Total vCPU", "value": int(sum(r.attributes.get("vcpus") or 0 for r in resources))},
        {"label": "Total RAM (GB)", "value": round(sum((r.attributes.get("ram_mb") or 0) / 1024 for r in resources), 1)},
    ]


def _evs_summary(resources: Sequence[Resource]) -> Summary:
    return [
        {"label": "Total volúmenes", "value": len(resources)},
        {"label": "Total (GB)", "value": int(sum(r.attributes.get("size_gb") or 0 for r in resources))},
    ]


def _merge(*builders: Callable[[Sequence[Resource]], Summary]) -> Callable[[Sequence[Resource]], Summary]:
    return lambda rs: [item for b in builders for item in b(rs)]


SUMMARIES: Dict[str, Callable[[Sequence[Resource]], Summary]] = {
    "ecs": _ecs_summary,
    "evs": _evs_summary,
    "vpc": _merge(_count("VPCs", "vpc.vpc"), _count("Subredes", "vpc.subnet"),
                  _count("Security Groups", "vpc.security_group")),
    "vpn": _merge(_count("VPN Gateways", "vpn.gateway"), _count("Conexiones", "vpn.connection")),
    "obs": _count("Buckets", "obs.bucket"),
    "eip": _count("Total EIP", "eip.publicip"),
    "cbr": _count("Total Vaults", "cbr.vault"),
    "elb": _count("Total ELB", "elb.loadbalancer"),
    "rds": _count("Total Instancias RDS", "rds.instance"),
    "dcs": _count("Total Instancias DCS", "dcs.instance"),
    "nat": _count("Total NAT Gateways", "nat.gateway"),
    "ces": _count("Total Alarmas", "ces.alarm_rule"),
    "hss": _count("Total Servidores", "hss.protected_server"),
    "waf": _count("Total Instancias WAF", "waf.instance"),
    "cfw": _count("Total Firewalls", "cfw.firewall"),
}

OTHER_REGIONS_LABEL = {"obs": "Buckets en otras regiones"}


def build_service_view(service: str, resources: Sequence[Resource], *,
                       out_of_region: int = 0) -> Tuple[List[Table], Summary]:
    """Tablas y KPIs de un servicio con el formato que consume ``static/app.js``."""
    tables = [spec.build(resources) for spec in TABLE_SPECS[service]]
    summary = SUMMARIES[service](resources)
    if out_of_region and service in OTHER_REGIONS_LABEL:
        summary.append({"label": OTHER_REGIONS_LABEL[service], "value": out_of_region})
    return tables, summary
