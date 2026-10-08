# coding: utf-8
"""Calculadora de precios standalone — sin selección de cliente ni cuenta.

Replica la experiencia de la calculadora oficial de Huawei Cloud: el usuario elige
región y producto y obtiene precios reales de lista directamente, sin configurar ningún
cliente. El sistema usa automáticamente cualquier cuenta activa que tenga un proyecto
en la región pedida para consultar los precios a la API de Huawei Cloud (BSS/ECS).

Prefijo ``/api/calculator``:

- ``GET  /options``          regiones, modos, duraciones, flavors y tipos de disco
- ``POST /quote``            precio de UN ítem (recalculado en el servidor)
- ``POST /list``             precios de la lista completa y su total
- ``POST /export``           la lista en ``xlsx``, ``pdf`` o ``csv``
- ``POST /prices/refresh``   consulta precios oficiales a Huawei (auto-credenciales)
- ``POST /flavors/refresh``  trae los flavors ECS de la región (auto-credenciales)
- ``POST /catalog/refresh``  flavors + tipos de disco de la región (auto-credenciales)
- ``GET  /rds/flavors``      catálogo informativo de flavors de RDS (sin precio)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.authz import Permission, Principal
from core.clients import ClientFactory
from core.crypto import SecretCipher
from core.errors import classify_exception
from costs import huawei_pricing
from costs.calculator import (
    MAX_ITEMS, MODES, OBS_CLASSES, CalcItem,
    item_from_payload, price_item, price_list, quotable,
)
from costs.catalog import PriceCatalog
from costs.configuration import DISK_TYPES, ConfigurationError
from costs.flavor_names import disco
from costs.huawei_pricing import RDS_ENGINES
from costs.region_comparison import project_for_region
from costs.region_simulation import flavor_dict
from db.models import CloudAccount
from db.session import get_db
from routers.cost_compare_api import MAX_QUOTES_PER_REFRESH, _regions
from routers.deps import get_cipher
from routers.security import get_principal, requires
from services.calculator_export import export_csv, export_pdf, export_xlsx
from tenancy.accounts import decrypt_credentials
from tenancy.errors import ValidationFailedError

router = APIRouter(prefix="/api/calculator", tags=["calculator-standalone"],
                   dependencies=[Depends(requires(Permission.COSTS_READ))])
CALLS_HUAWEI = [Depends(requires(Permission.SCANS_RUN))]
EXPORT_TYPES = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "pdf": "application/pdf", "csv": "text/csv; charset=utf-8"}


class ItemsIn(BaseModel):
    items: List[Dict[str, Any]] = Field(..., min_length=1, max_length=MAX_ITEMS)


# ---------------------------------------------------------------- helpers

def _items(db: Session, payloads: List[Dict[str, Any]]) -> List[CalcItem]:
    known = [r.id for r in _regions(db)]
    try:
        return [item_from_payload(p, known) for p in payloads]
    except (ConfigurationError, ValueError) as exc:
        raise ValidationFailedError(str(exc)) from None


def _auto_clients(db: Session, cipher: SecretCipher, region: str):
    """Finds any active account with a project in the given region.

    Returns (ClientFactory, project) or (None, None) if none found.
    """
    accounts = list(db.scalars(
        select(CloudAccount).where(CloudAccount.status == "active")
    ))
    for account in accounts:
        project = project_for_region(db, account.id, region)
        if project is None:
            continue
        try:
            creds = decrypt_credentials(account, cipher)
            return ClientFactory(creds, endpoint_domain=account.endpoint_domain), project
        except Exception:
            continue
    return None, None


def _has_project_for_region(db: Session, region: str) -> bool:
    """True if any active account has a project in this region."""
    for account in db.scalars(select(CloudAccount).where(CloudAccount.status == "active")):
        if project_for_region(db, account.id, region) is not None:
            return True
    return False


def _priced(db: Session, items: List[CalcItem]) -> List[Dict[str, Any]]:
    catalog = PriceCatalog(db)
    return [price_item(catalog, item, has_project=_has_project_for_region(db, item.region))
            for item in items]


def _safe_error(exc: Exception, service: str, region: str, clients: ClientFactory) -> Dict[str, Any]:
    error = classify_exception(exc, service=service, region=region, secrets=clients.secrets)
    return {"servicio": service, "mensaje": error.message, "categoria": error.kind,
            "http_status": error.http_status, "error_code": error.error_code,
            "request_id": error.request_id, "accion_iam": error.iam_action}


# ---------------------------------------------------------------- endpoints de lectura

@router.get("/options")
def options(region: Optional[str] = Query(None, max_length=64),
            billing_mode: str = Query("monthly", pattern="^(monthly|yearly|on_demand)$"),
            os_type: str = Query("linux", pattern="^(linux|windows)$"),
            db: Session = Depends(get_db)) -> Dict[str, Any]:
    regions = _regions(db)
    known = {r.id for r in regions}
    result: Dict[str, Any] = {
        "regions": [{"id": r.id, "name": r.display_name,
                     "has_project": _has_project_for_region(db, r.id)} for r in regions],
        "modes": [{"id": k, **v} for k, v in MODES.items()],
        "disk_types": [{"id": d, "name": disco(d)} for d in DISK_TYPES],
        "max_items": MAX_ITEMS,
        "obs": {"classes": list(OBS_CLASSES)},
    }
    if region:
        if region not in known:
            raise ValidationFailedError("Región desconocida.")
        has_project = _has_project_for_region(db, region)
        catalog = PriceCatalog(db)
        flavors = []
        for f in catalog.flavors(region):
            row = flavor_dict(f)
            price = catalog.lookup(region=region, product="ecs", spec=f"{f.flavor_id}.{os_type}",
                                   billing_mode=billing_mode)
            row["price"] = ({"amount": str(price.amount), "currency": price.currency, "period": price.period}
                            if price is not None else None)
            flavors.append(row)
        volumes = {v.name.upper(): v for v in catalog.volume_types(region)}
        result.update({
            "region": region, "has_project": has_project, "flavors": flavors,
            "flavors_updated_at": max((f.fetched_at for f in catalog.flavors(region)), default=None),
            "volume_catalog": bool(volumes),
            "disk_availability": {d: (None if not volumes else
                                      ("no_existe" if d not in volumes else
                                       ("agotado" if volumes[d].availability_zones and not
                                        [z for z in volumes[d].availability_zones
                                         if z not in set(volumes[d].sold_out_zones or [])] else "disponible")))
                                  for d in DISK_TYPES},
        })
    return result


@router.post("/quote")
def quote(body: Dict[str, Any], db: Session = Depends(get_db)) -> Dict[str, Any]:
    item = _items(db, [body])[0]
    priced = _priced(db, [item])[0]
    return {k: v for k, v in priced.items() if not k.startswith("_")}


@router.post("/list")
def price_items(body: ItemsIn, db: Session = Depends(get_db)) -> Dict[str, Any]:
    return price_list(_priced(db, _items(db, body.items)))


@router.post("/export")
def export(body: ItemsIn, format: str = Query("xlsx", pattern="^(xlsx|pdf|csv)$"),
           db: Session = Depends(get_db)) -> Response:
    priced = price_list(_priced(db, _items(db, body.items)))
    regions = {r.id: r.display_name for r in _regions(db)}
    stamp = datetime.now()
    builder = {"xlsx": export_xlsx, "pdf": export_pdf, "csv": export_csv}[format]
    content = builder(priced, regions=regions, generated_at=stamp)
    name = f"Calculadora_Huawei_{stamp:%Y%m%d_%H%M}.{format}"
    return Response(content=content, media_type=EXPORT_TYPES[format],
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- refresh con auto-credenciales

@router.post("/prices/refresh", dependencies=CALLS_HUAWEI)
def refresh_prices(body: ItemsIn, db: Session = Depends(get_db),
                   cipher: SecretCipher = Depends(get_cipher),
                   actor: Principal = Depends(get_principal)):
    """Consulta precios oficiales a Huawei BSS usando la primera cuenta activa con proyecto
    en cada región. Mismo resultado que la calculadora oficial de Huawei Cloud."""
    items = _items(db, body.items)
    wanted = quotable(items)
    if sum(len(c) for c in wanted.values()) > MAX_QUOTES_PER_REFRESH:
        raise ValidationFailedError(f"Demasiados componentes distintos (máximo {MAX_QUOTES_PER_REFRESH}).")
    store = PriceCatalog(db)
    stored, failures = 0, []
    for (region, mode), components in sorted(wanted.items()):
        clients, project = _auto_clients(db, cipher, region)
        if project is None:
            failures += [{"component": c.label, "region": region,
                          "reason": "Sin cuenta activa con proyecto en esta región."} for c in components]
            continue
        quotes, failed = huawei_pricing.fetch_quotes(
            clients, project_id=project.huawei_project_id, region=region,
            components=components, billing_mode=mode,
            client_builder=huawei_pricing.default_bss_builder)
        stored += store.store(quotes)
        failures += failed
    return {"stored": stored, "failures": failures}


@router.post("/flavors/refresh", dependencies=CALLS_HUAWEI)
def refresh_flavors(region: Optional[str] = Query(None, max_length=64),
                    db: Session = Depends(get_db), cipher: SecretCipher = Depends(get_cipher),
                    actor: Principal = Depends(get_principal)) -> Dict[str, Any]:
    """Trae los flavors ECS de Huawei Cloud usando la primera cuenta activa con proyecto
    en cada región (o en la región indicada)."""
    all_regions = _regions(db)
    targets = [r for r in all_regions if r.id == region] if region else all_regions
    if region and not targets:
        raise ValidationFailedError("Región desconocida.")
    store, now, stored_map, failures = PriceCatalog(db), datetime.now(timezone.utc), {}, []
    for r in targets:
        clients, project = _auto_clients(db, cipher, r.id)
        if project is None:
            failures.append({"region": r.id, "reason": "Sin cuenta activa con proyecto en esta región."})
            continue
        try:
            flavors = huawei_pricing.fetch_flavors(clients, region=r.id,
                                                   project_id=project.huawei_project_id)
            stored_map[r.id] = store.replace_flavors(r.id, flavors, fetched_at=now)
        except Exception as exc:
            error = classify_exception(exc, service="ecs", region=r.id, secrets=clients.secrets)
            failures.append({"region": r.id, "error": error.kind, "message": error.message,
                             "error_code": error.error_code})
    return {"stored": stored_map, "failures": failures}


@router.post("/catalog/refresh", dependencies=CALLS_HUAWEI)
def refresh_catalog(region: str = Query(..., max_length=64),
                    db: Session = Depends(get_db), cipher: SecretCipher = Depends(get_cipher),
                    actor: Principal = Depends(get_principal)) -> Dict[str, Any]:
    """Consulta a Huawei los flavors (ECS) y tipos de disco (EVS) de la región,
    usando la primera cuenta activa con proyecto en ella."""
    if region not in {r.id for r in _regions(db)}:
        raise ValidationFailedError("Región desconocida.")
    clients, project = _auto_clients(db, cipher, region)
    if project is None:
        raise ValidationFailedError(f"Sin cuenta activa con proyecto en {region}.")
    store, now = PriceCatalog(db), datetime.now(timezone.utc)
    result: Dict[str, Any] = {"region": region, "flavors": None, "volume_types": None, "errors": []}
    try:
        flavors = huawei_pricing.fetch_flavors(clients, region=region,
                                               project_id=project.huawei_project_id,
                                               client_builder=huawei_pricing.default_ecs_builder)
        result["flavors"] = store.replace_flavors(region, flavors, fetched_at=now)
    except Exception as exc:
        result["errors"].append(_safe_error(exc, "ecs", region, clients))
    try:
        volumes = huawei_pricing.fetch_volume_types(clients, region=region,
                                                    project_id=project.huawei_project_id,
                                                    client_builder=huawei_pricing.default_evs_builder)
        result["volume_types"] = store.replace_volume_types(region, volumes, fetched_at=now)
    except Exception as exc:
        result["errors"].append(_safe_error(exc, "evs", region, clients))
    return result


# ---------------------------------------------------------------- RDS (informativo)

@router.get("/rds/flavors")
def rds_flavors(region: str = Query(..., max_length=64), engine: str = Query(...),
                version: Optional[str] = Query(None, max_length=32),
                db: Session = Depends(get_db)) -> Dict[str, Any]:
    if engine not in RDS_ENGINES:
        raise ValidationFailedError(f"Motor no válido ({', '.join(RDS_ENGINES)}).")
    if region not in {r.id for r in _regions(db)}:
        raise ValidationFailedError("Región desconocida.")
    catalog = PriceCatalog(db)
    versions = catalog.rds_versions(region, engine)
    version = version if version in versions else (versions[0] if versions else None)
    rows = catalog.rds_flavors(region, engine, version) if version else []
    flavors = [{"spec_code": f.spec_code, "vcpus": f.vcpus, "ram_gb": float(str(f.ram_gb)),
                "instance_mode": f.instance_mode, "az_status": f.az_status or {},
                "available": any(v == "normal" for v in (f.az_status or {}).values()) or not f.az_status}
               for f in rows]
    return {"region": region, "engine": engine, "versions": versions, "version": version,
            "has_project": _has_project_for_region(db, region), "flavors": flavors,
            "updated_at": max((f.fetched_at for f in rows), default=None),
            "pricing_supported": False,
            "note": "Catálogo informativo: Huawei Cloud no permite consultar precios de RDS por API."}
