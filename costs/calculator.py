# coding: utf-8
"""Calculadora de precios: ítems configurados libremente (ECS completo, EVS, EIP…) con
precio oficial de lista × duración × cantidad, y una lista con su total.

Reglas (las mismas del resto de la plataforma; nada se inventa):
- Precio unitario SOLO del catálogo de precios oficiales (BSS ``official_website_amount``)
  para el MISMO modo de cobro: el anual sale del precio anual oficial, nunca de 12 × mes.
- Un ítem solo tiene total si todos sus componentes tienen precio y una sola moneda.
- El total de la lista solo suma ítems completos (separado por moneda); los demás se cuentan.
- Sin Project en la región: no se puede cotizar (Huawei exige el project_id para BSS).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Set, Sequence, Tuple

from costs.catalog import PriceCatalog
from costs.configuration import BANDWIDTH_MODES, DISK_TYPES, IP_TYPE_RE, Component, ConfigurationError
from costs.flavor_names import disco
from costs.region_simulation import config_from_payload

CENT = Decimal("0.01")
PRODUCTS = ("ecs", "evs", "eip", "rds", "obs")
RDS_ENGINES = ("MySQL", "PostgreSQL", "SQLServer")
RDS_MODES = ("single", "ha", "replica")
RDS_STORAGE = ("COMMON", "ULTRAHIGH", "CLOUDSSD", "ESSD", "LOCALSSD")
OBS_CLASSES = ("STANDARD", "WARM", "COLD")
SPEC_RE = re.compile(r"^[a-z0-9][a-z0-9._\-]{2,99}$")
BSS_CODE_RE = re.compile(r"^hws\.[A-Za-z0-9._\-]{3,120}$")
USAGE_CODE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
# Productos cotizados SIEMPRE por uso (precio por GB/unidad), sea cual sea el modo del ítem.
USAGE_PRODUCTS = ("traffic", "obs_storage")
MODES: Dict[str, Dict[str, Any]] = {
    # duración: unidad, mínimo, máximo (los mismos rangos que ofrece la consola de Huawei Cloud)
    "monthly": {"label": "Mensual", "unit": "mes", "units": "meses", "min": 1, "max": 9, "period": "month"},
    "yearly": {"label": "Anual", "unit": "año", "units": "años", "min": 1, "max": 3, "period": "year"},
    "on_demand": {"label": "Por uso", "unit": "hora", "units": "horas", "min": 1, "max": 8784, "period": "hour"},
}
MAX_QUANTITY = 100
MAX_ITEMS = 50
MAX_TRAFFIC_GB = 1_000_000
UNIT_LABEL = {"hour": "h", "month": "mes", "year": "año", "gb": "GB"}


def _money(value: Optional[Decimal]) -> Optional[str]:
    return str(value.quantize(CENT, ROUND_HALF_UP)) if value is not None else None


def _int(value: Any, label: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ConfigurationError(f"{label}: debe ser un número entero.") from None
    if not low <= number <= high:
        raise ConfigurationError(f"{label}: debe estar entre {low} y {high}.")
    return number


@dataclass
class Line:
    component: Component
    usage: Optional[int] = None          # GB de tráfico estimados (solo ``traffic``)
    unit_amount: Optional[Decimal] = None
    unit_period: Optional[str] = None
    currency: Optional[str] = None
    multiplier: Decimal = Decimal("1")
    subtotal: Optional[Decimal] = None
    source: Optional[str] = None
    updated_at: Optional[str] = None
    reason: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        c = self.component
        return {"label": c.label, "product": c.product, "spec": c.spec, "size": c.size,
                "units_per_item": c.quantity, "usage_gb": self.usage, "available": self.subtotal is not None,
                "unit_amount": _plain(self.unit_amount), "unit": UNIT_LABEL.get(self.unit_period or "", None),
                "currency": self.currency, "multiplier": _plain(self.multiplier),
                "subtotal": _money(self.subtotal), "source": self.source, "updated_at": self.updated_at,
                "reason": self.reason}


def _plain(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return text


@dataclass
class CalcItem:
    region: str
    billing_mode: str
    duration: int
    quantity: int
    product: str
    config: Dict[str, Any]
    name: str = ""
    traffic_gb: int = 0
    _components: List[Component] = field(default_factory=list, repr=False)

    def components(self) -> List[Component]:
        return list(self._components)

    def summary(self) -> str:
        c = self.config
        if self.product == "ecs":
            parts = [f"{c['flavor']} ({'Windows' if c['os_type'] == 'windows' else 'Linux'})",
                     f"sistema {disco(c['system_disk']['volume_type'])} {c['system_disk']['size_gb']} GB"]
            parts += [f"datos {disco(d['volume_type'])} {d['size_gb']} GB" for d in c.get("data_disks", [])]
            if c.get("eip"):
                parts.append(_eip_text(c["eip"]))
            return " · ".join(parts)
        if self.product == "evs":
            return f"{disco(c['volume_type'])} {c['size_gb']} GB"
        if self.product == "rds":
            modes = {"single": "instancia única", "ha": "primaria/en espera", "replica": "réplica de lectura"}
            return (f"{c['engine']} {c['version']} · {c['spec_code']} ({modes[c['instance_mode']]}) · "
                    f"almacenamiento {c['storage_type']} {c['storage_gb']} GB")
        if self.product == "obs":
            return f"OBS {c['storage_class']} · {c['storage_gb']} GB"
        return _eip_text(c)

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "region": self.region, "billing_mode": self.billing_mode, "duration": self.duration,
                "quantity": self.quantity, "product": self.product, "config": self.config,
                "traffic_gb": self.traffic_gb, "summary": self.summary(),
                "duration_text": duration_text(self.billing_mode, self.duration)}


def duration_text(mode: str, duration: int) -> str:
    info = MODES[mode]
    return f"{duration} {info['unit'] if duration == 1 else info['units']}"


def _eip_text(eip: Dict[str, Any]) -> str:
    mode = "por tráfico" if eip["bandwidth_mode"] == "traffic" else "por ancho de banda"
    return f"EIP {eip['ip_type']} · {eip['bandwidth_mbps']} Mbps {mode}"


def _eip(payload: Any, label: str = "EIP") -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ConfigurationError(f"{label}: formato inválido.")
    ip_type = str(payload.get("ip_type") or "5_bgp").strip().lower()
    if not IP_TYPE_RE.match(ip_type):
        raise ConfigurationError(f"{label}: tipo no válido (p. ej. 5_bgp).")
    mode = str(payload.get("bandwidth_mode") or "bandwidth").strip().lower()
    if mode not in BANDWIDTH_MODES:
        raise ConfigurationError(f"{label}: cobro no válido (bandwidth o traffic).")
    return {"ip_type": ip_type, "bandwidth_mode": mode,
            "bandwidth_mbps": _int(payload.get("bandwidth_mbps"), f"{label} (Mbps)", 1, 2000)}


def _eip_components(eip: Dict[str, Any]) -> List[Component]:
    suffix = eip["ip_type"].split("_", 1)[1]
    parts = [Component("ip", eip["ip_type"], label=f"IP pública {eip['ip_type']}")]
    if eip["bandwidth_mode"] == "traffic":
        parts.append(Component("traffic", f"12_{suffix}", size=eip["bandwidth_mbps"],
                               label=f"Tráfico de salida (máx. {eip['bandwidth_mbps']} Mbps)"))
    else:
        parts.append(Component("bandwidth", f"19_{suffix}", size=eip["bandwidth_mbps"],
                               label=f"Ancho de banda {eip['bandwidth_mbps']} Mbps"))
    return parts


def _codes(raw: Any, keys: Sequence[str], confirmed: Optional[Dict[str, Set[str]]]) -> Tuple[Dict[str, str], Optional[str]]:
    """Códigos BSS elegidos para RDS/OBS. Deben existir en la lista oficial guardada (ListServiceTypes/
    ListResourceTypes/ListUsageTypes); si no, el componente queda sin precio con el motivo."""
    raw = raw if isinstance(raw, dict) else {}
    codes: Dict[str, str] = {}
    for key in keys:
        value = str(raw.get(key) or "").strip()
        pattern = USAGE_CODE_RE if key == "usage" else BSS_CODE_RE
        if value and not pattern.match(value):
            raise ConfigurationError(f"Código BSS no válido ({key}).")
        codes[key] = value
    if any(not codes[k] for k in keys):
        return codes, "faltan los códigos BSS del producto: actualízalos desde Huawei y elígelos"
    if confirmed is not None:
        kind_of = {"service": "service", "usage": "usage"}
        for key in keys:
            if codes[key] not in confirmed.get(kind_of.get(key, "resource"), set()):
                return codes, f"el código BSS {codes[key]} no está en la lista oficial consultada a Huawei"
    return codes, None


def item_from_payload(payload: Dict[str, Any], known_regions: Sequence[str],
                      confirmed_codes: Optional[Dict[str, Set[str]]] = None) -> CalcItem:
    """Valida un ítem de la calculadora y construye sus componentes.

    ``confirmed_codes``: códigos BSS oficiales guardados (para RDS/OBS); ``None`` = no se comprueban."""
    if not isinstance(payload, dict):
        raise ConfigurationError("Ítem inválido.")
    region = str(payload.get("region") or "").strip()
    if region not in known_regions:
        raise ConfigurationError("Región desconocida.")
    mode = str(payload.get("billing_mode") or "monthly")
    if mode not in MODES:
        raise ConfigurationError("Modo de cobro no válido (mensual, anual o por uso).")
    info = MODES[mode]
    duration = _int(payload.get("duration", 1), f"Duración ({info['units']})", info["min"], info["max"])
    quantity = _int(payload.get("quantity", 1), "Cantidad", 1, MAX_QUANTITY)
    product = str(payload.get("product") or "")
    if product not in PRODUCTS:
        raise ConfigurationError(f"Producto no válido ({', '.join(PRODUCTS)}).")
    raw = payload.get("config") or {}
    name = str(payload.get("name") or "").strip()[:80]
    traffic_gb = 0
    if product == "ecs":
        sim = config_from_payload(raw)
        config: Dict[str, Any] = {"flavor": sim.flavor, "os_type": sim.os_type,
                                  "system_disk": {"volume_type": sim.system_disk.volume_type,
                                                  "size_gb": sim.system_disk.size_gb},
                                  "data_disks": [{"volume_type": d.volume_type, "size_gb": d.size_gb}
                                                 for d in sim.data_disks], "eip": None}
        comps = [Component("ecs", f"{sim.flavor}.{sim.os_type}", label=f"ECS {sim.flavor} ({sim.os_type})"),
                 Component("evs", sim.system_disk.volume_type, size=sim.system_disk.size_gb,
                           label=f"Disco de sistema {sim.system_disk.volume_type} {sim.system_disk.size_gb} GB")]
        comps += [Component("evs", d.volume_type, size=d.size_gb,
                            label=f"Disco de datos {i} {d.volume_type} {d.size_gb} GB")
                  for i, d in enumerate(sim.data_disks, start=1)]
        if raw.get("eip"):
            config["eip"] = _eip(raw["eip"])
            comps += _eip_components(config["eip"])
            if config["eip"]["bandwidth_mode"] == "traffic":
                traffic_gb = _int(raw["eip"].get("traffic_gb", 0), "Tráfico estimado (GB)", 0, MAX_TRAFFIC_GB)
    elif product == "rds":
        engine = str(raw.get("engine") or "")
        if engine not in RDS_ENGINES:
            raise ConfigurationError(f"Motor no válido ({', '.join(RDS_ENGINES)}).")
        version = str(raw.get("version") or "").strip()
        if not re.match(r"^[0-9][0-9A-Za-z._\-]{0,31}$", version):
            raise ConfigurationError("Versión del motor no válida.")
        mode_rds = str(raw.get("instance_mode") or "single")
        if mode_rds not in RDS_MODES:
            raise ConfigurationError("Tipo de instancia no válido (single, ha o replica).")
        spec = str(raw.get("spec_code") or "").strip().lower()
        if not SPEC_RE.match(spec):
            raise ConfigurationError("Flavor de RDS no válido.")
        storage_type = str(raw.get("storage_type") or "CLOUDSSD").strip().upper()
        if storage_type not in RDS_STORAGE:
            raise ConfigurationError(f"Almacenamiento no válido ({', '.join(RDS_STORAGE)}).")
        storage_gb = _int(raw.get("storage_gb", 40), "Almacenamiento (GB)", 40, 4000)
        codes, problem = _codes(raw.get("codes"), ("service", "instance", "storage"), confirmed_codes)
        config = {"engine": engine, "version": version, "instance_mode": mode_rds, "spec_code": spec,
                  "storage_type": storage_type, "storage_gb": storage_gb, "codes": codes}
        comps = [Component("rds", spec, label=f"RDS {engine} {spec}", problem=problem,
                           codes=(codes["service"], codes["instance"], None) if not problem else None),
                 Component("rds_storage", storage_type, size=storage_gb,
                           label=f"Almacenamiento RDS {storage_type} {storage_gb} GB", problem=problem,
                           codes=(codes["service"], codes["storage"], 17) if not problem else None)]
    elif product == "obs":
        storage_class = str(raw.get("storage_class") or "STANDARD").strip().upper()
        if storage_class not in OBS_CLASSES:
            raise ConfigurationError(f"Clase de almacenamiento no válida ({', '.join(OBS_CLASSES)}).")
        storage_gb = _int(raw.get("storage_gb", 100), "Almacenamiento (GB)", 1, 10_000_000)
        measure_id = _int(raw.get("measure_id", 10), "Unidad de medida", 1, 1000)
        codes, problem = _codes(raw.get("codes"), ("service", "resource", "usage"), confirmed_codes)
        config = {"storage_class": storage_class, "storage_gb": storage_gb, "measure_id": measure_id, "codes": codes}
        comps = [Component("obs_storage", storage_class, label=f"OBS {storage_class} {storage_gb} GB", problem=problem,
                           codes=(codes["service"], codes["resource"], None) if not problem else None,
                           usage=(codes["usage"], measure_id) if not problem else None)]
    elif product == "evs":
        volume_type = str(raw.get("volume_type") or "").strip().upper()
        if volume_type not in DISK_TYPES:
            raise ConfigurationError(f"Tipo de disco no válido (usa {', '.join(DISK_TYPES)}).")
        size = _int(raw.get("size_gb"), "Tamaño (GB)", 10, 32768)
        config = {"volume_type": volume_type, "size_gb": size}
        comps = [Component("evs", volume_type, size=size, label=f"Disco {volume_type} {size} GB")]
    else:
        config = _eip(raw)
        comps = _eip_components(config)
        if config["bandwidth_mode"] == "traffic":
            traffic_gb = _int(raw.get("traffic_gb", 0), "Tráfico estimado (GB)", 0, MAX_TRAFFIC_GB)
    return CalcItem(region=region, billing_mode=mode, duration=duration, quantity=quantity, product=product,
                    config=config, name=name, traffic_gb=traffic_gb, _components=comps)


def lookup_mode(component: Component, mode: str) -> str:
    """Tráfico y OBS se cobran siempre por uso (precio por GB/unidad), sea cual sea el modo del ítem."""
    return "on_demand" if component.product in USAGE_PRODUCTS else mode


def price_line(catalog: PriceCatalog, item: CalcItem, component: Component) -> Line:
    line = Line(component)
    if component.problem:
        line.reason = f"Precio no disponible: {component.problem}."
        return line
    mode = lookup_mode(component, item.billing_mode)
    entry = catalog.lookup(region=item.region, product=component.product, spec=component.spec or "",
                           billing_mode=mode, size=component.size)
    if entry is None:
        line.reason = (f"Precio no disponible: no hay precio oficial de {component.label} en {item.region} "
                       f"({MODES[mode]['label'].lower()}). Pulsa «Consultar precios oficiales».")
        return line
    line.unit_amount = Decimal(str(entry.amount))
    line.unit_period, line.currency = entry.period, entry.currency
    line.source = f"Huawei Cloud BSS ({entry.source_detail})"
    line.updated_at = entry.fetched_at.isoformat() if entry.fetched_at else None
    if component.product == "traffic":
        line.usage = item.traffic_gb
        line.multiplier = Decimal(item.traffic_gb)
    elif component.product == "obs_storage":
        line.usage = item.config["storage_gb"]
        line.multiplier = Decimal(item.config["storage_gb"])
    else:
        line.multiplier = Decimal(item.duration)
    line.subtotal = line.unit_amount * line.multiplier * component.quantity * item.quantity
    return line


def price_item(catalog: PriceCatalog, item: CalcItem, *, has_project: bool) -> Dict[str, Any]:
    lines = [price_line(catalog, item, c) for c in item.components()]
    missing = [l.component.label for l in lines if l.subtotal is None]
    currencies = {l.currency for l in lines if l.subtotal is not None}
    total, currency, reason = None, None, None
    if missing:
        reason = "Precio no disponible para: " + ", ".join(missing)
    elif len(currencies) > 1:
        reason = "Los componentes tienen monedas distintas; no se suman."
    else:
        currency = currencies.pop() if currencies else None
        total = sum((l.subtotal for l in lines if l.subtotal is not None), Decimal("0"))
    warnings = []
    if not has_project:
        warnings.append("Sin Project en la cuenta para esta región: no se pueden consultar precios oficiales.")
    if any(l.component.product == "traffic" for l in lines) and not item.traffic_gb:
        warnings.append("Ancho de banda por tráfico: indica los GB estimados; con 0 GB su costo es 0.")
    if item.product == "obs":
        warnings.append("OBS: Huawei no documenta la cotización de OBS por API. Se muestra el precio que BSS "
                        "devuelve para 1 unidad del tipo de uso elegido × GB; valídalo con la consola.")
    return {**item.as_dict(), "has_project": has_project, "lines": [l.as_dict() for l in lines],
            "total": _money(total), "currency": currency, "complete": total is not None, "reason": reason,
            "warnings": warnings, "_total": total}


def price_list(priced: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Total por moneda SOLO de los ítems completos."""
    totals: Dict[str, Decimal] = {}
    for item in priced:
        if item["_total"] is not None:
            totals[item["currency"] or ""] = totals.get(item["currency"] or "", Decimal("0")) + item["_total"]
    return {"items": [{k: v for k, v in i.items() if not k.startswith("_")} for i in priced],
            "totals": [{"currency": c, "total": _money(t)} for c, t in sorted(totals.items())],
            "complete": sum(1 for i in priced if i["_total"] is not None),
            "incomplete": sum(1 for i in priced if i["_total"] is None)}


def quotable(items: Sequence[CalcItem]) -> Dict[Tuple[str, str], List[Component]]:
    """Componentes a cotizar agrupados por ``(región, modo de cobro BSS)`` sin duplicados."""
    wanted: Dict[Tuple[str, str], Dict[Tuple[str, str, int], Component]] = {}
    for item in items:
        for c in item.components():
            if c.problem or not c.spec:
                continue
            key = (item.region, lookup_mode(c, item.billing_mode))
            wanted.setdefault(key, {})[(c.product, c.spec or "", c.size)] = Component(
                c.product, c.spec, size=c.size, label=c.label, codes=c.codes, usage=c.usage)
    return {k: list(v.values()) for k, v in wanted.items()}
