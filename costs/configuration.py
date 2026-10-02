# coding: utf-8
"""Configuración de un recurso para estimar su costo (origen real o destino simulado).

- ``origin_from_inventory``: construye la configuración de ORIGEN a partir del
  inventario real (ECS + sus discos EVS + sus EIP; o un EVS/EIP suelto). Solo lee.
- ``destination_from_payload``: configuración de DESTINO editable por el usuario,
  validada con listas blancas (nunca se usa como endpoint ni toca Huawei).
- ``ResourceConfiguration.components``: piezas con precio propio (ECS, disco, IP,
  ancho de banda) identificadas con los códigos de producto de Huawei.

Nada se supone en silencio: lo que el inventario no dice (p. ej. el sistema
operativo o el modo de cobro del ancho de banda) se indica en ``notes``.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.validation import InvalidValueError, validate_region_id
from db.models import InventoryResource

SUPPORTED_ORIGINS = {"ecs.server": "ecs", "evs.volume": "evs", "eip.publicip": "eip"}
OS_TYPES = ("linux", "windows")
DISK_TYPES = ("SATA", "SAS", "GPSSD", "SSD", "ESSD", "GPSSD2", "ESSD2")
BANDWIDTH_MODES = ("bandwidth", "traffic")
FLAVOR_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,62}$")
IP_TYPE_RE = re.compile(r"^5_[a-z0-9]{2,16}$")
MAX_DISKS, MAX_EIPS = 24, 10


class ConfigurationError(ValueError):
    """Configuración inválida o recurso no soportado (mensaje seguro para el usuario)."""


@dataclass(frozen=True)
class Disk:
    volume_type: Optional[str]
    size_gb: int
    name: Optional[str] = None


@dataclass(frozen=True)
class PublicIp:
    ip_type: Optional[str]                # p. ej. 5_bgp (campo ``type`` del EIP)
    bandwidth_mbps: Optional[int]
    bandwidth_mode: Optional[str] = "bandwidth"   # bandwidth | traffic | None (desconocido)
    shared: bool = False                  # ancho de banda compartido: se cotiza aparte
    address: Optional[str] = None


@dataclass(frozen=True)
class Component:
    """Pieza con precio propio. ``size`` = GB o Mbps (0 si el producto no es lineal)."""

    product: str            # ecs | evs | ip | bandwidth
    spec: Optional[str]     # resource_spec de Huawei; None = no determinable
    size: int = 0
    quantity: int = 1
    label: str = ""
    problem: Optional[str] = None   # por qué no se puede cotizar (sin inventar)


@dataclass
class ResourceConfiguration:
    kind: str                        # ecs | evs | eip
    region: str
    flavor: Optional[str] = None
    vcpus: Optional[int] = None
    ram_gb: Optional[float] = None
    os_type: Optional[str] = None
    disks: List[Disk] = field(default_factory=list)
    public_ips: List[PublicIp] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def components(self) -> List[Component]:
        parts: List[Component] = []
        if self.kind == "ecs":
            problem = None
            if not self.flavor:
                problem = "flavor desconocido"
            elif not self.os_type:
                problem = "sistema operativo desconocido: elige Linux o Windows"
            parts.append(Component("ecs", f"{self.flavor}.{self.os_type}" if not problem else None,
                                   label=f"ECS {self.flavor or '?'} ({self.os_type or 'SO ?'})", problem=problem))
        for disk in self.disks:
            parts.append(Component("evs", disk.volume_type, size=disk.size_gb,
                                   label=f"EVS {disk.volume_type or '?'} {disk.size_gb} GB",
                                   problem=None if disk.volume_type else "tipo de disco desconocido"))
        for ip in self.public_ips:
            parts.append(Component("ip", ip.ip_type, label=f"EIP {ip.ip_type or '?'}",
                                   problem=None if ip.ip_type else "tipo de IP desconocido"))
            parts.append(_bandwidth(ip))
        return parts

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "region": self.region, "flavor": self.flavor, "vcpus": self.vcpus,
                "ram_gb": self.ram_gb, "os_type": self.os_type,
                "disks": [{"volume_type": d.volume_type, "size_gb": d.size_gb, "name": d.name} for d in self.disks],
                "public_ips": [{"ip_type": i.ip_type, "bandwidth_mbps": i.bandwidth_mbps,
                                "bandwidth_mode": i.bandwidth_mode, "shared": i.shared, "address": i.address}
                               for i in self.public_ips],
                "notes": list(self.notes)}


def _bandwidth(ip: PublicIp) -> Component:
    label = f"Ancho de banda {ip.bandwidth_mbps or '?'} Mbps"
    if ip.shared:
        return Component("bandwidth", None, label=label + " (compartido)",
                         problem="ancho de banda compartido: se factura aparte y no se incluye")
    if ip.bandwidth_mode == "traffic":
        return Component("bandwidth", None, label=label + " (por tráfico)",
                         problem="cobro por tráfico: depende de los GB transferidos, no hay precio mensual fijo")
    if not ip.bandwidth_mbps or not ip.ip_type or ip.bandwidth_mode is None:
        return Component("bandwidth", None, label=label, problem="tamaño o modo de cobro del ancho de banda desconocido")
    # Línea "19_<tipo>" = ancho de banda cobrado por tamaño (código de Huawei; p. ej. 5_bgp -> 19_bgp).
    return Component("bandwidth", "19_" + ip.ip_type.split("_", 1)[1], size=ip.bandwidth_mbps, label=label)


# ---------------------------------------------------------------- origen (inventario real, solo lectura)
def _os_type(attributes: Dict[str, Any]) -> Optional[str]:
    value = " ".join(str(attributes.get(k) or "") for k in ("os_type", "image_name", "os")).lower()
    if "win" in value:
        return "windows"
    if any(word in value for word in ("linux", "ubuntu", "centos", "debian", "euler", "suse", "red hat", "rocky")):
        return "linux"
    return None


def _int(value: Any) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _disk(row: InventoryResource) -> Disk:
    attributes = row.attributes or {}
    return Disk(volume_type=attributes.get("volume_type"), size_gb=_int(attributes.get("size_gb")) or 0,
                name=row.name or row.provider_id)


def _public_ip(row: InventoryResource) -> PublicIp:
    attributes = row.attributes or {}
    share = str(attributes.get("bandwidth_share_type") or "").upper()
    return PublicIp(ip_type=attributes.get("type"), bandwidth_mbps=_int(attributes.get("bandwidth_size")),
                    bandwidth_mode=None, shared=share == "WHOLE", address=attributes.get("public_ip"))


def _active(session: Session, account_id: uuid.UUID, resource_type: str) -> Sequence[InventoryResource]:
    return session.scalars(select(InventoryResource).where(
        InventoryResource.account_id == account_id, InventoryResource.resource_type == resource_type,
        InventoryResource.deleted_at.is_(None))).all()


def origin_from_inventory(session: Session, resource: InventoryResource) -> ResourceConfiguration:
    """Configuración real del recurso según el último inventario. No modifica nada."""
    kind = SUPPORTED_ORIGINS.get(resource.resource_type)
    if kind is None:
        raise ConfigurationError(f"El comparador aún no admite recursos de tipo {resource.resource_type} "
                                 "(soportados: ECS, EVS y EIP).")
    attributes = resource.attributes or {}
    config = ResourceConfiguration(kind=kind, region=resource.region)
    if resource.deleted_at is not None:
        config.notes.append("El recurso ya no existe en Huawei Cloud; se usa su última configuración conocida.")
    if kind == "evs":
        config.disks = [_disk(resource)]
    elif kind == "eip":
        config.public_ips = [_public_ip(resource)]
    else:
        config.flavor = attributes.get("flavor_name") or attributes.get("flavor_id")
        config.vcpus = _int(attributes.get("vcpus"))
        ram_mb = _int(attributes.get("ram_mb"))
        config.ram_gb = round(ram_mb / 1024, 2) if ram_mb else None
        config.os_type = _os_type(attributes)
        if config.os_type is None:
            config.notes.append("El inventario no indica el sistema operativo: elígelo para poder cotizar el ECS.")
        volume_ids = set(attributes.get("volume_ids") or [])
        config.disks = [_disk(v) for v in _active(session, resource.account_id, "evs.volume")
                        if v.provider_id in volume_ids
                        or resource.provider_id in ((v.attributes or {}).get("server_ids") or [])]
        public = set(attributes.get("public_ips") or [])
        config.public_ips = [_public_ip(e) for e in _active(session, resource.account_id, "eip.publicip")
                             if (e.attributes or {}).get("public_ip") in public]
    if any(ip.bandwidth_mode is None and not ip.shared for ip in config.public_ips):
        config.notes.append("El inventario no indica si el ancho de banda se cobra por tamaño o por tráfico: "
                            "indícalo para poder cotizarlo.")
    return config


def complete_origin(origin: ResourceConfiguration, assumptions: Optional[Dict[str, Any]]) -> ResourceConfiguration:
    """Completa SOLO los datos que el inventario no tiene (SO, modo de cobro del ancho de
    banda). Nunca sobrescribe un dato real; cada dato aportado queda anotado."""
    assumptions = assumptions or {}
    unknown = set(assumptions) - {"os_type", "bandwidth_mode"}
    if unknown:
        raise ConfigurationError("Solo se pueden completar os_type y bandwidth_mode del origen.")
    config = replace(origin, notes=list(origin.notes))
    os_type = _choice(assumptions.get("os_type"), OS_TYPES, "Sistema operativo")
    if os_type and config.kind == "ecs" and config.os_type is None:
        config.os_type = os_type
        config.notes.append(f"Sistema operativo del origen indicado por el usuario: {os_type}.")
    mode = _choice(assumptions.get("bandwidth_mode"), BANDWIDTH_MODES, "Modo de cobro")
    if mode and any(ip.bandwidth_mode is None for ip in config.public_ips):
        config.public_ips = [replace(ip, bandwidth_mode=mode) if ip.bandwidth_mode is None else ip
                             for ip in config.public_ips]
        config.notes.append(f"Modo de cobro del ancho de banda del origen indicado por el usuario: {mode}.")
    return config


# ---------------------------------------------------------------- destino (editable, validado)
def _choice(value: Any, allowed: Sequence[str], label: str) -> Optional[str]:
    if value in (None, ""):
        return None
    if value not in allowed:
        raise ConfigurationError(f"{label} no válido.")
    return str(value)


def _bounded(value: Any, low: float, high: float, label: str) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ConfigurationError(f"{label} debe ser un número.") from None
    if not low <= number <= high:
        raise ConfigurationError(f"{label} fuera de rango ({low:g}–{high:g}).")
    return number


def destination_from_payload(origin: ResourceConfiguration, payload: Optional[Dict[str, Any]],
                             *, region: str, known_regions: Sequence[str]) -> ResourceConfiguration:
    """Destino = copia del origen en ``region`` con los cambios de ``payload``.

    Es solo una simulación de costos: no modifica el recurso real ni el inventario.
    """
    payload = payload or {}
    try:
        region = validate_region_id(payload.get("region") or region)
    except InvalidValueError:
        raise ConfigurationError("Región de destino no válida.") from None
    if region not in known_regions:
        raise ConfigurationError(f"Región de destino desconocida: {region}.")
    config = replace(origin, region=region, notes=[])
    if origin.kind == "ecs":
        if "flavor" in payload and payload["flavor"]:
            flavor = str(payload["flavor"]).strip().lower()
            if not FLAVOR_RE.match(flavor):
                raise ConfigurationError("Flavor no válido.")
            config.flavor = flavor
        if "os_type" in payload:
            config.os_type = _choice(payload.get("os_type"), OS_TYPES, "Sistema operativo") or origin.os_type
        vcpus = _bounded(payload.get("vcpus"), 1, 512, "vCPU")
        ram = _bounded(payload.get("ram_gb"), 0.5, 12288, "RAM (GB)")
        config.vcpus = int(vcpus) if vcpus is not None else config.vcpus
        config.ram_gb = ram if ram is not None else config.ram_gb
    if "disks" in payload:
        disks = payload.get("disks") or []
        if not isinstance(disks, list) or len(disks) > MAX_DISKS:
            raise ConfigurationError(f"Máximo {MAX_DISKS} discos.")
        config.disks = [Disk(volume_type=_choice(d.get("volume_type"), DISK_TYPES, "Tipo de disco"),
                             size_gb=int(_bounded(d.get("size_gb"), 1, 65536, "Tamaño de disco (GB)") or 0),
                             name=str(d.get("name") or "")[:100] or None)
                        for d in disks if isinstance(d, dict)]
    if "public_ips" in payload:
        ips = payload.get("public_ips") or []
        if not isinstance(ips, list) or len(ips) > MAX_EIPS:
            raise ConfigurationError(f"Máximo {MAX_EIPS} EIP.")
        parsed = []
        for ip in ips:
            if not isinstance(ip, dict):
                continue
            ip_type = ip.get("ip_type") or None
            if ip_type is not None and not IP_TYPE_RE.match(str(ip_type)):
                raise ConfigurationError("Tipo de EIP no válido.")
            mbps = _bounded(ip.get("bandwidth_mbps"), 1, 2000, "Ancho de banda (Mbps)")
            parsed.append(PublicIp(ip_type=ip_type, bandwidth_mbps=int(mbps) if mbps else None,
                                   bandwidth_mode=_choice(ip.get("bandwidth_mode"), BANDWIDTH_MODES, "Modo de cobro"),
                                   shared=bool(ip.get("shared", False))))
        config.public_ips = parsed
    return config
