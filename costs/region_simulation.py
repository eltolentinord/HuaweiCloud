# coding: utf-8
"""RegionSimulation: una configuración LIBRE (no tiene por qué existir en la cuenta) cotizada
en varias regiones a la vez, con la disponibilidad oficial de su flavor y sus discos.

Solo lectura: no toca el inventario ni Huawei Cloud (los precios y catálogos se leen de
la base; se actualizan aparte con las consultas oficiales). Nada se inventa: sin catálogo
consultado se dice «sin catálogo», sin precio «Precio no disponible», sin Project «Sin
Project en la cuenta».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from costs.catalog import PriceCatalog
from costs.comparison import SideCost, difference, money
from costs.configuration import (
    BANDWIDTH_MODES,
    DISK_TYPES,
    FLAVOR_RE,
    IP_TYPE_RE,
    OS_TYPES,
    Component,
    ConfigurationError,
    Disk,
    PublicIp,
    ResourceConfiguration,
)
from costs.flavor_names import VENDIBLES, descripcion_flavor, disco, estado, familia
from costs.region_comparison import project_for_region
from costs.resolver import BILLING_LABELS, PriceResolver
from db.models import CloudAccount, FlavorCatalogEntry

MAX_REGIONS = 6
MAX_DATA_DISKS = 8
MAX_EQUIVALENTS = 5


@dataclass
class SimulationConfig:
    flavor: str
    os_type: str
    system_disk: Disk
    data_disks: List[Disk] = field(default_factory=list)
    eip: Optional[PublicIp] = None

    def for_region(self, region: str) -> ResourceConfiguration:
        ips = [self.eip] if self.eip else []
        return ResourceConfiguration(kind="ecs", region=region, flavor=self.flavor, os_type=self.os_type,
                                     disks=[self.system_disk, *self.data_disks], public_ips=ips)

    def as_dict(self) -> Dict[str, Any]:
        return self.for_region("").as_dict()


def _int(value: Any, label: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ConfigurationError(f"{label}: debe ser un número entero.") from None
    if not low <= number <= high:
        raise ConfigurationError(f"{label}: debe estar entre {low} y {high}.")
    return number


def _disk(payload: Any, label: str) -> Disk:
    if not isinstance(payload, dict):
        raise ConfigurationError(f"{label}: formato inválido.")
    volume_type = str(payload.get("volume_type") or "").strip().upper()
    if volume_type not in DISK_TYPES:
        raise ConfigurationError(f"{label}: tipo de disco no válido (usa {', '.join(DISK_TYPES)}).")
    return Disk(volume_type=volume_type, size_gb=_int(payload.get("size_gb"), f"{label} (GB)", 10, 32768), name=label)


def config_from_payload(payload: Dict[str, Any]) -> SimulationConfig:
    """Valida la configuración con las mismas reglas que el comparador desde un recurso."""
    if not isinstance(payload, dict):
        raise ConfigurationError("Configuración inválida.")
    flavor = str(payload.get("flavor") or "").strip().lower()
    if not FLAVOR_RE.match(flavor):
        raise ConfigurationError("Flavor no válido (p. ej. s6.large.2 o x0.4u.8g).")
    os_type = str(payload.get("os_type") or "").strip().lower()
    if os_type not in OS_TYPES:
        raise ConfigurationError("Sistema operativo: elige Linux o Windows.")
    system_disk = _disk(payload.get("system_disk"), "Disco de sistema")
    raw_data = payload.get("data_disks") or []
    if not isinstance(raw_data, list) or len(raw_data) > MAX_DATA_DISKS:
        raise ConfigurationError(f"Máximo {MAX_DATA_DISKS} discos de datos.")
    data_disks = [_disk(d, f"Disco de datos {i}") for i, d in enumerate(raw_data, start=1)]
    eip = None
    raw_eip = payload.get("eip")
    if raw_eip:
        if not isinstance(raw_eip, dict):
            raise ConfigurationError("EIP: formato inválido.")
        ip_type = str(raw_eip.get("ip_type") or "5_bgp").strip().lower()
        if not IP_TYPE_RE.match(ip_type):
            raise ConfigurationError("EIP: tipo no válido (p. ej. 5_bgp).")
        mode = str(raw_eip.get("bandwidth_mode") or "bandwidth").strip().lower()
        if mode not in BANDWIDTH_MODES:
            raise ConfigurationError("EIP: modo de cobro no válido (bandwidth o traffic).")
        eip = PublicIp(ip_type=ip_type, bandwidth_mbps=_int(raw_eip.get("bandwidth_mbps"), "EIP (Mbps)", 1, 2000),
                       bandwidth_mode=mode)
    return SimulationConfig(flavor=flavor, os_type=os_type, system_disk=system_disk, data_disks=data_disks, eip=eip)


def flavor_dict(f: FlavorCatalogEntry) -> Dict[str, Any]:
    status = f.status or "normal"
    return {"flavor_id": f.flavor_id, "vcpus": f.vcpus, "ram_gb": round(f.ram_mb / 1024, 2),
            "performance_type": f.performance_type, "familia": familia(f.performance_type),
            "generation": f.generation, "status": status, "estado": estado(status),
            "available": status in VENDIBLES, "az_status": f.az_status or {},
            "architecture": f.architecture, "cpu_name": f.cpu_name, "gpu_name": f.gpu_name,
            "max_bandwidth_gbps": str(f.max_bandwidth_gbps) if f.max_bandwidth_gbps is not None else None,
            "max_pps": f.max_pps, "descripcion": descripcion_flavor(f.vcpus, f.ram_mb, f.performance_type),
            "fetched_at": f.fetched_at}


class RegionSimulation:
    def __init__(self, session: Session, account: CloudAccount, resolver: PriceResolver,
                 known_regions: Sequence[str]) -> None:
        self.session = session
        self.account = account
        self.resolver = resolver
        self.catalog: PriceCatalog = resolver.catalog
        self.known_regions = list(known_regions)

    def _regions(self, regions: Sequence[str]) -> List[str]:
        unique = list(dict.fromkeys(r.strip() for r in regions if r and r.strip()))
        if not unique:
            raise ConfigurationError("Elige al menos una región.")
        if len(unique) > MAX_REGIONS:
            raise ConfigurationError(f"Máximo {MAX_REGIONS} regiones por comparación.")
        unknown = [r for r in unique if r not in self.known_regions]
        if unknown:
            raise ConfigurationError(f"Región desconocida: {', '.join(unknown)}.")
        return unique

    def _flavor_check(self, config: SimulationConfig, region: str) -> Dict[str, Any]:
        flavors = self.catalog.flavors(region)
        if not flavors:
            return {"state": "sin_catalogo", "label": "Sin catálogo consultado", "flavor": None, "equivalents": [],
                    "message": "No hay lista de flavors consultada para esta región: actualízala en «Catálogo por región»."}
        match = next((f for f in flavors if f.flavor_id == config.flavor), None)
        reference = match or self._reference(config.flavor)
        equivalents = []
        if reference is not None:
            same = [f for f in flavors if f.vcpus == reference.vcpus and f.ram_mb == reference.ram_mb
                    and f.flavor_id != config.flavor and (f.status or "normal") in VENDIBLES]
            same.sort(key=lambda f: (f.performance_type != reference.performance_type, f.flavor_id))
            equivalents = [flavor_dict(f) for f in same[:MAX_EQUIVALENTS]]
        if match is None:
            return {"state": "no_existe", "label": "No existe en la región", "flavor": None, "equivalents": equivalents,
                    "message": f"El flavor {config.flavor} no aparece en la lista oficial de esta región."}
        status = match.status or "normal"
        state = "disponible" if status in ("normal", "promotion") else "agotado" if status == "sellout" else "beta"
        message = None
        if state == "agotado":
            message = f"{config.flavor} está agotado en esta región."
        elif state == "beta":
            message = f"{config.flavor} está en beta pública en esta región."
        return {"state": state, "label": estado(status), "flavor": flavor_dict(match), "equivalents": equivalents,
                "message": message}

    def _reference(self, flavor_id: str) -> Optional[FlavorCatalogEntry]:
        """vCPU/RAM del flavor según el catálogo de cualquier otra región (para sugerir equivalentes)."""
        for region in self.known_regions:
            found = self.catalog.flavor(region, flavor_id)
            if found is not None:
                return found
        return None

    def _disk_checks(self, config: SimulationConfig, region: str) -> List[Dict[str, Any]]:
        known = {t.name.upper(): t for t in self.catalog.volume_types(region)}
        checks = []
        for d in [config.system_disk, *config.data_disks]:
            entry = known.get((d.volume_type or "").upper())
            if not known:
                state, label = "sin_catalogo", "Sin catálogo consultado"
            elif entry is None:
                state, label = "no_existe", "No existe en la región"
            else:
                free = [z for z in entry.availability_zones if z not in set(entry.sold_out_zones)]
                state, label = ("disponible", "Disponible") if free or not entry.availability_zones \
                    else ("agotado", "Agotado en todas las zonas")
            checks.append({"name": d.name, "volume_type": d.volume_type, "tipo": disco(d.volume_type),
                           "size_gb": d.size_gb, "state": state, "label": label})
        return checks

    def simulate(self, config: SimulationConfig, regions: Sequence[str]) -> Dict[str, Any]:
        results = []
        for region in self._regions(regions):
            region_config = config.for_region(region)
            side = SideCost.from_lines([self.resolver.price(c, region) for c in region_config.components()])
            flavor = self._flavor_check(config, region)
            disks = self._disk_checks(config, region)
            has_project = project_for_region(self.session, self.account.id, region) is not None
            warnings = [m for m in [flavor.get("message")] if m]
            warnings += [f"{d['name']}: {d['tipo']} — {d['label'].lower()}." for d in disks
                         if d["state"] in ("no_existe", "agotado")]
            if not has_project:
                warnings.append("Sin Project en la cuenta: no se pueden consultar precios ni catálogo de esta región.")
            results.append({"region": region, "has_project": has_project, "flavor_check": flavor,
                            "disk_checks": disks, "cost": side.as_dict(), "warnings": warnings,
                            "_total": side.total, "_currency": side.currency})
        return {"config": config.as_dict(), "regions": [self._public(r) for r in results],
                "summary": self._summary(results)}

    @staticmethod
    def _public(result: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in result.items() if not k.startswith("_")}

    def _summary(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """La más barata SOLO entre regiones con precio completo y la misma moneda que la primera completa."""
        complete = [r for r in results if r["_total"] is not None]
        currency = complete[0]["_currency"] if complete else None
        same = [r for r in complete if r["_currency"] == currency]
        cheapest = min(same, key=lambda r: r["_total"], default=None)
        reference = results[0] if results else None
        deltas = []
        if reference is not None and reference["_total"] is not None:
            for r in same:
                absolute, percent = difference(reference["_total"], r["_total"])
                deltas.append({"region": r["region"], "difference": money(absolute),
                               "difference_percent": str(percent) if percent is not None else None})
        return {"reference_region": reference["region"] if reference else None,
                "cheapest_region": cheapest["region"] if cheapest else None,
                "cheapest_total": money(cheapest["_total"]) if cheapest else None, "currency": currency,
                "complete_regions": [r["region"] for r in same],
                "incomplete_regions": [r["region"] for r in results if r not in same],
                "differences": deltas, "billing_mode": self.resolver.billing_mode,
                "billing_label": BILLING_LABELS[self.resolver.billing_mode],
                "period": "mensual" + (f" ({self.resolver.hours_per_month} h de uso)"
                                       if self.resolver.billing_mode == "on_demand" else " (1 mes de suscripción)")}

    def quotable(self, config: SimulationConfig, regions: Sequence[str]) -> Dict[str, List[Component]]:
        """Componentes cotizables por región (sin duplicados) para la consulta oficial a BSS."""
        wanted: Dict[str, List[Component]] = {}
        for region in self._regions(regions):
            seen: Dict[tuple, Component] = {}
            for c in config.for_region(region).components():
                if c.spec and not c.problem:
                    seen[(c.product, c.spec, c.size)] = Component(c.product, c.spec, size=c.size, label=c.label)
            wanted[region] = list(seen.values())
        return wanted
