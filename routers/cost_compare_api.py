# coding: utf-8
"""Comparador de costos por región (simulación; nunca modifica recursos ni inventario).

Prefijo ``/api/clients/{client_id}/accounts/{account_id}/cost-compare``:

- ``GET  /options``          regiones, flavors conocidos de una región y opciones editables
- ``GET  /origin/{id}``      configuración de ORIGEN extraída del inventario real
- ``POST /compare``          comparación origen/destino de uno o varios recursos (JSON)
- ``POST /compare/export``   la misma comparación en ``xlsx`` o ``csv``
- ``POST /prices/refresh``   consulta precios OFICIALES a Huawei (BSS) y los guarda   [scans:run]
- ``POST /flavors/refresh``  consulta los flavors ECS de una región (ListFlavors)       [scans:run]
- ``GET  /catalog``          catálogo de una región: flavors (estado, zonas…) y tipos de disco (de la base)
- ``POST /catalog/refresh``  consulta ListFlavors + CinderListVolumeTypes de una región  [scans:run]
- ``POST /simulate``         configuración LIBRE (no tiene que existir) en varias regiones
- ``POST /simulate/prices/refresh``  precios oficiales (BSS) de esa configuración       [scans:run]

Lectura: ``costs:read``. Las dos consultas a Huawei exigen ``scans:run`` (como lanzar un
escaneo), usan las credenciales cifradas de la cuenta y quedan en la auditoría.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.authz import Permission, Principal
from core.clients import ClientFactory
from core.crypto import SecretCipher
from core.errors import classify_exception
from costs import huawei_pricing, pricing
from costs.catalog import PriceCatalog
from costs.configuration import BANDWIDTH_MODES, DISK_TYPES, OS_TYPES, ConfigurationError
from costs.flavor_names import disco
from costs.region_comparison import RegionPriceComparison, project_for_region, quotable_components
from costs.region_simulation import MAX_REGIONS, RegionSimulation, config_from_payload, flavor_dict
from costs.resolver import BILLING_LABELS, DEFAULT_HOURS_PER_MONTH, PriceResolver
from db.models import Region
from db.session import get_db
from exports.inventory_export import table, to_csv, to_xlsx
from routers.deps import get_cipher
from routers.exports_api import _download
from routers.security import get_principal, requires
from tenancy import audit
from tenancy.accounts import decrypt_credentials, get_account
from tenancy.errors import ValidationFailedError

router = APIRouter(prefix="/api/clients/{client_id}/accounts/{account_id}/cost-compare", tags=["cost-compare"],
                   dependencies=[Depends(requires(Permission.COSTS_READ))])
CALLS_HUAWEI = [Depends(requires(Permission.SCANS_RUN))]
MAX_QUOTES_PER_REFRESH = 120  # una consulta BSS por componente distinto: límite de llamadas por petición


class CompareItemIn(BaseModel):
    resource_id: uuid.UUID
    destination: Optional[Dict[str, Any]] = Field(None, description="Cambios sobre el origen (simulación)")
    origin_assumptions: Optional[Dict[str, Any]] = Field(
        None, description="Solo datos que el inventario no tiene: os_type, bandwidth_mode")


class CompareIn(BaseModel):
    region: str = Field(..., max_length=64, description="Región destino por defecto")
    billing_mode: str = Field("monthly", pattern="^(monthly|on_demand)$")
    hours_per_month: int = Field(DEFAULT_HOURS_PER_MONTH, ge=1, le=744)
    items: List[CompareItemIn] = Field(..., min_length=1, max_length=50)


class SimulateIn(BaseModel):
    regions: List[str] = Field(..., min_length=1, max_length=MAX_REGIONS)
    billing_mode: str = Field("monthly", pattern="^(monthly|on_demand)$")
    hours_per_month: int = Field(DEFAULT_HOURS_PER_MONTH, ge=1, le=744)
    config: Dict[str, Any]


def _regions(db: Session) -> List[Region]:
    return list(db.scalars(select(Region).where(Region.is_supported.is_(True))
                           .order_by(Region.sort_order, Region.id)))


def _service(db: Session, client_id: uuid.UUID, account_id: uuid.UUID, billing_mode: str = "monthly",
             hours: int = DEFAULT_HOURS_PER_MONTH) -> RegionPriceComparison:
    account = get_account(db, client_id, account_id)
    try:
        table_provider = pricing.provider_from_env()
    except pricing.PriceTableError as exc:
        raise ValidationFailedError(f"Tabla de precios inválida: {exc}") from None
    resolver = PriceResolver(PriceCatalog(db), billing_mode=billing_mode, hours_per_month=hours,
                             table=table_provider)
    return RegionPriceComparison(db, account, resolver, [r.id for r in _regions(db)])


def _compare(db: Session, client_id: uuid.UUID, account_id: uuid.UUID, body: CompareIn):
    service = _service(db, client_id, account_id, body.billing_mode, body.hours_per_month)
    try:
        items = service.compare([(i.resource_id, i.destination, i.origin_assumptions) for i in body.items],
                                region=body.region)
    except (ConfigurationError, ValueError) as exc:
        raise ValidationFailedError(str(exc)) from None
    return service, items


@router.get("/options")
def options(client_id: uuid.UUID, account_id: uuid.UUID, region: Optional[str] = Query(None, max_length=64),
            db: Session = Depends(get_db)) -> Dict[str, Any]:
    account = get_account(db, client_id, account_id)
    catalog = PriceCatalog(db)
    prices = catalog.counts_by_region()
    project_regions = {p.region_id for p in [project_for_region(db, account.id, r.id) for r in _regions(db)] if p}
    flavors = catalog.flavors(region) if region else []
    try:
        table_name = pricing.provider_from_env().name
    except pricing.PriceTableError as exc:
        table_name = f"inválida: {exc}"
    return {
        "regions": [{"id": r.id, "name": r.display_name, "has_project": r.id in project_regions,
                     "official_prices": prices.get(r.id, 0)} for r in _regions(db)],
        "flavors": [{"flavor_id": f.flavor_id, "vcpus": f.vcpus, "ram_gb": round(f.ram_mb / 1024, 2),
                     "performance_type": f.performance_type, "generation": f.generation} for f in flavors],
        "flavors_updated_at": max((f.fetched_at for f in flavors), default=None),
        "billing_modes": [{"id": k, "label": v} for k, v in BILLING_LABELS.items()],
        "os_types": list(OS_TYPES), "disk_types": list(DISK_TYPES), "bandwidth_modes": list(BANDWIDTH_MODES),
        "hours_per_month": DEFAULT_HOURS_PER_MONTH, "price_table": table_name,
    }


@router.get("/origin/{resource_id}")
def origin(client_id: uuid.UUID, account_id: uuid.UUID, resource_id: uuid.UUID,
           db: Session = Depends(get_db)) -> Dict[str, Any]:
    service = _service(db, client_id, account_id)
    try:
        row, config = service.origin(resource_id)
    except ConfigurationError as exc:
        raise ValidationFailedError(str(exc)) from None
    return {"resource": {"id": str(row.id), "name": row.name, "provider_id": row.provider_id, "service": row.service,
                         "resource_type": row.resource_type, "region": row.region, "status": row.status,
                         "last_seen": row.last_seen},
            "config": config.as_dict()}


@router.post("/compare")
def compare(client_id: uuid.UUID, account_id: uuid.UUID, body: CompareIn,
            db: Session = Depends(get_db)) -> Dict[str, Any]:
    service, items = _compare(db, client_id, account_id, body)
    return {"summary": service.summary(items), "items": [i.as_dict() for i in items],
            "generated_at": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------- exportación (sistema existente)
COMPARE_COLUMNS = ["Recurso", "Servicio", "Región origen", "Región destino", "Origen", "Destino",
                   "Total origen", "Total destino", "Moneda", "Diferencia", "Diferencia %", "Observaciones"]
DETAIL_COLUMNS = ["Recurso", "Lado", "Componente", "Código", "Importe mensual", "Moneda", "Base",
                  "Fuente", "Actualizado", "Motivo"]


def _describe(config: Dict[str, Any]) -> str:
    parts = []
    if config.get("flavor"):
        parts.append(f"{config['flavor']} ({config.get('vcpus') or '?'} vCPU, {config.get('ram_gb') or '?'} GB, "
                     f"{config.get('os_type') or 'SO ?'})")
    parts += [f"EVS {d['volume_type'] or '?'} {d['size_gb']} GB" for d in config["disks"]]
    parts += [f"EIP {i['ip_type'] or '?'} {i['bandwidth_mbps'] or '?'} Mbps" for i in config["public_ips"]]
    return "; ".join(parts)


@router.post("/compare/export")
def export(client_id: uuid.UUID, account_id: uuid.UUID, body: CompareIn,
           format: str = Query("xlsx", pattern="^(xlsx|csv)$"), db: Session = Depends(get_db)):
    service, items = _compare(db, client_id, account_id, body)
    rows, details = [], []
    for item in (i.as_dict() for i in items):
        name = item["resource"]["name"] or item["resource"]["provider_id"]
        rows.append({"Recurso": name, "Servicio": item["resource"]["service"],
                     "Región origen": item["origin_config"]["region"],
                     "Región destino": item["destination_config"]["region"],
                     "Origen": _describe(item["origin_config"]), "Destino": _describe(item["destination_config"]),
                     "Total origen": item["origin"]["total"] or "no disponible",
                     "Total destino": item["destination"]["total"] or "no disponible",
                     "Moneda": item["currency"] or "", "Diferencia": item["difference"] or "",
                     "Diferencia %": item["difference_percent"] or "",
                     "Observaciones": " ".join(filter(None, [item["reason"], *item["warnings"]]))})
        for side, label in (("origin", "Origen"), ("destination", "Destino")):
            for line in item[side]["lines"]:
                details.append({"Recurso": name, "Lado": label, "Componente": line["label"], "Código": line["spec"] or "",
                                "Importe mensual": line["monthly_amount"] or "no disponible",
                                "Moneda": line["currency"] or "", "Base": line["basis"], "Fuente": line["source"] or "",
                                "Actualizado": line["updated_at"] or "", "Motivo": line["reason"] or ""})
    account_name = service.account.name
    if format == "csv":
        return _download(to_csv(COMPARE_COLUMNS, rows), format, account_name, "comparacion_regiones")
    summary = service.summary(items)
    summary_rows = [{"Concepto": "Modo de cobro", "Valor": summary["billing_label"]},
                    {"Concepto": "Período", "Valor": summary["period"]},
                    {"Concepto": "Recursos comparados", "Valor": f"{summary['compared']} de {summary['items']}"}]
    for group in summary["by_currency"]:
        summary_rows += [{"Concepto": f"Total origen ({group['currency']})", "Valor": group["origin"]},
                         {"Concepto": f"Total destino ({group['currency']})", "Valor": group["destination"]},
                         {"Concepto": f"Diferencia ({group['currency']})", "Valor": group["difference"]},
                         {"Concepto": "Diferencia %", "Valor": group["difference_percent"] or ""}]
    return _download(to_xlsx([table("Resumen", summary_rows, ["Concepto", "Valor"]),
                              table("Comparación", rows, COMPARE_COLUMNS),
                              table("Detalle de precios", details, DETAIL_COLUMNS)]),
                     format, account_name, "comparacion_regiones")


# ---------------------------------------------------------------- consultas oficiales a Huawei
def _clients(db: Session, cipher: SecretCipher, client_id: uuid.UUID, account_id: uuid.UUID) -> ClientFactory:
    account = get_account(db, client_id, account_id)
    return ClientFactory(decrypt_credentials(account, cipher), endpoint_domain=account.endpoint_domain)


@router.post("/prices/refresh", dependencies=CALLS_HUAWEI)
def refresh_prices(client_id: uuid.UUID, account_id: uuid.UUID, body: CompareIn, db: Session = Depends(get_db),
                   cipher: SecretCipher = Depends(get_cipher), actor: Principal = Depends(get_principal)):
    service, items = _compare(db, client_id, account_id, body)
    wanted = quotable_components(items)
    if sum(len(c) for c in wanted.values()) > MAX_QUOTES_PER_REFRESH:
        raise ValidationFailedError(f"Demasiados componentes distintos para una consulta (máximo "
                                    f"{MAX_QUOTES_PER_REFRESH}); compara menos recursos a la vez.")
    clients = _clients(db, cipher, client_id, account_id)
    catalog = PriceCatalog(db)
    stored, failures = 0, []
    for region, components in sorted(wanted.items()):
        project = project_for_region(db, service.account.id, region)
        if project is None:
            failures += [{"component": c.label, "region": region,
                          "reason": f"La cuenta no tiene Project ID registrado en {region} (necesario para cotizar)."}
                         for c in components]
            continue
        quotes, failed = huawei_pricing.fetch_quotes(
            clients, project_id=project.huawei_project_id, region=region, components=components,
            billing_mode=body.billing_mode, client_builder=huawei_pricing.default_bss_builder)
        stored += catalog.store(quotes)
        failures += failed
    audit.record(db, actor, "prices.refresh", client_id=client_id, account_id=service.account.id,
                 details={"regions": sorted(wanted), "fields": [f"stored={stored}",
                                                                                   f"failed={len(failures)}"]})
    return {"stored": stored, "failures": failures}


@router.post("/flavors/refresh", dependencies=CALLS_HUAWEI)
def refresh_flavors(client_id: uuid.UUID, account_id: uuid.UUID, region: str = Query(..., max_length=64),
                    db: Session = Depends(get_db), cipher: SecretCipher = Depends(get_cipher),
                    actor: Principal = Depends(get_principal)):
    if region not in {r.id for r in _regions(db)}:
        raise ValidationFailedError("Región desconocida.")
    account = get_account(db, client_id, account_id)
    project = project_for_region(db, account.id, region)
    if project is None:
        raise ValidationFailedError(f"La cuenta no tiene Project ID registrado en {region}.")
    clients = _clients(db, cipher, client_id, account_id)
    try:
        flavors = huawei_pricing.fetch_flavors(clients, region=region, project_id=project.huawei_project_id,
                                               client_builder=huawei_pricing.default_ecs_builder)
    except Exception as exc:  # clasificado y redactado: nunca AK/SK
        error = classify_exception(exc, service="ecs", region=region, secrets=clients.secrets)
        raise HTTPException(status_code=502, detail={"mensaje": error.message, "categoria": error.kind,
                                                     "http_status": error.http_status, "error_code": error.error_code,
                                                     "accion_iam": error.iam_action})
    count = PriceCatalog(db).replace_flavors(region, flavors, fetched_at=datetime.now(timezone.utc))
    audit.record(db, actor, "flavors.refresh", client_id=client_id, account_id=account.id,
                 details={"region_id": region, "fields": [f"flavors={count}"]})
    return {"region": region, "flavors": count}


# ---------------------------------------------------------------- catálogo por región
def _known_region(db: Session, region: str) -> str:
    if region not in {r.id for r in _regions(db)}:
        raise ValidationFailedError("Región desconocida.")
    return region


@router.get("/catalog")
def catalog(client_id: uuid.UUID, account_id: uuid.UUID, region: str = Query(..., max_length=64),
            db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Flavors y tipos de disco de la región tal como los devolvió Huawei la última vez (no llama a Huawei)."""
    account = get_account(db, client_id, account_id)
    region = _known_region(db, region)
    store = PriceCatalog(db)
    flavors = store.flavors(region)
    volumes = store.volume_types(region)
    return {
        "region": region,
        "has_project": project_for_region(db, account.id, region) is not None,
        "flavors": [flavor_dict(f) for f in flavors],
        "flavors_updated_at": max((f.fetched_at for f in flavors), default=None),
        "volume_types": [{"name": v.name, "tipo": disco(v.name), "availability_zones": v.availability_zones or [],
                          "sold_out_zones": v.sold_out_zones or [],
                          "available_zones": [z for z in (v.availability_zones or [])
                                              if z not in set(v.sold_out_zones or [])]} for v in volumes],
        "volume_types_updated_at": max((v.fetched_at for v in volumes), default=None),
    }


def _safe_error(exc: Exception, service: str, region: str, clients: ClientFactory) -> Dict[str, Any]:
    error = classify_exception(exc, service=service, region=region, secrets=clients.secrets)
    return {"servicio": service, "mensaje": error.message, "categoria": error.kind, "http_status": error.http_status,
            "error_code": error.error_code, "request_id": error.request_id, "accion_iam": error.iam_action}


@router.post("/catalog/refresh", dependencies=CALLS_HUAWEI)
def refresh_catalog(client_id: uuid.UUID, account_id: uuid.UUID, region: str = Query(..., max_length=64),
                    db: Session = Depends(get_db), cipher: SecretCipher = Depends(get_cipher),
                    actor: Principal = Depends(get_principal)):
    """Consulta a Huawei los flavors (ECS) y los tipos de disco (EVS) de la región. Si una de las
    dos consultas falla, la otra se guarda igualmente y el error se devuelve clasificado."""
    region = _known_region(db, region)
    account = get_account(db, client_id, account_id)
    project = project_for_region(db, account.id, region)
    if project is None:
        raise ValidationFailedError(f"Sin Project en la cuenta para {region}: no se puede consultar el catálogo.")
    clients = _clients(db, cipher, client_id, account_id)
    store, now = PriceCatalog(db), datetime.now(timezone.utc)
    result: Dict[str, Any] = {"region": region, "flavors": None, "volume_types": None, "errors": []}
    try:
        flavors = huawei_pricing.fetch_flavors(clients, region=region, project_id=project.huawei_project_id,
                                               client_builder=huawei_pricing.default_ecs_builder)
        result["flavors"] = store.replace_flavors(region, flavors, fetched_at=now)
    except Exception as exc:  # clasificado y redactado: nunca AK/SK
        result["errors"].append(_safe_error(exc, "ecs", region, clients))
    try:
        volumes = huawei_pricing.fetch_volume_types(clients, region=region, project_id=project.huawei_project_id,
                                                    client_builder=huawei_pricing.default_evs_builder)
        result["volume_types"] = store.replace_volume_types(region, volumes, fetched_at=now)
    except Exception as exc:
        result["errors"].append(_safe_error(exc, "evs", region, clients))
    audit.record(db, actor, "catalog.refresh", client_id=client_id, account_id=account.id,
                 details={"region_id": region, "fields": [f"flavors={result['flavors']}",
                                                          f"volume_types={result['volume_types']}",
                                                          f"errors={len(result['errors'])}"]})
    if result["flavors"] is None and result["volume_types"] is None:
        raise HTTPException(status_code=502, detail=result["errors"][0])
    return result


# ---------------------------------------------------------------- configuración libre
def _simulation(db: Session, client_id: uuid.UUID, account_id: uuid.UUID, body: SimulateIn):
    service = _service(db, client_id, account_id, body.billing_mode, body.hours_per_month)
    simulation = RegionSimulation(db, service.account, service.resolver, [r.id for r in _regions(db)])
    try:
        config = config_from_payload(body.config)
        simulation._regions(body.regions)  # valida antes de calcular
    except (ConfigurationError, ValueError) as exc:
        raise ValidationFailedError(str(exc)) from None
    return simulation, config


@router.post("/simulate")
def simulate(client_id: uuid.UUID, account_id: uuid.UUID, body: SimulateIn,
             db: Session = Depends(get_db)) -> Dict[str, Any]:
    simulation, config = _simulation(db, client_id, account_id, body)
    return simulation.simulate(config, body.regions)


@router.post("/simulate/prices/refresh", dependencies=CALLS_HUAWEI)
def refresh_simulation_prices(client_id: uuid.UUID, account_id: uuid.UUID, body: SimulateIn,
                              db: Session = Depends(get_db), cipher: SecretCipher = Depends(get_cipher),
                              actor: Principal = Depends(get_principal)):
    simulation, config = _simulation(db, client_id, account_id, body)
    wanted = simulation.quotable(config, body.regions)
    if sum(len(c) for c in wanted.values()) > MAX_QUOTES_PER_REFRESH:
        raise ValidationFailedError(f"Demasiados componentes distintos para una consulta (máximo "
                                    f"{MAX_QUOTES_PER_REFRESH}).")
    clients = _clients(db, cipher, client_id, account_id)
    store = PriceCatalog(db)
    stored, failures = 0, []
    for region, components in wanted.items():
        project = project_for_region(db, simulation.account.id, region)
        if project is None:
            failures += [{"component": c.label, "region": region,
                          "reason": "Sin Project en la cuenta: no se puede cotizar en esta región."}
                         for c in components]
            continue
        quotes, failed = huawei_pricing.fetch_quotes(
            clients, project_id=project.huawei_project_id, region=region, components=components,
            billing_mode=body.billing_mode, client_builder=huawei_pricing.default_bss_builder)
        stored += store.store(quotes)
        failures += failed
    audit.record(db, actor, "prices.refresh", client_id=client_id, account_id=simulation.account.id,
                 details={"regions": sorted(wanted), "fields": ["mode=simulation", f"stored={stored}",
                                                                f"failed={len(failures)}"]})
    return {"stored": stored, "failures": failures}
