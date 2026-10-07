# coding: utf-8
"""Calculadora de precios (simulación; nunca crea ni modifica recursos).

Prefijo ``/api/clients/{client_id}/accounts/{account_id}/calculator``:

- ``GET  /options``          regiones (con/sin Project), modos y duraciones, flavors ECS y discos
                             de la región con su precio oficial guardado (si lo hay)
- ``POST /quote``            precio de UN ítem (ECS completo, EVS o EIP)
- ``POST /list``             precios de la lista completa y su total (recalculado en el servidor)
- ``POST /flavors/refresh``  trae los flavors ECS reales de Huawei para la región (o todas) [scans:run]
- ``POST /prices/refresh``   consulta precios OFICIALES a Huawei (BSS) para los ítems   [scans:run]
- ``POST /export``           la lista en ``xlsx``, ``pdf`` o ``csv`` (recalculada en el servidor)
- ``GET  /rds/flavors``      flavors de RDS guardados (motor/versión/modo), SIN precio: Huawei no
                             permite cotizar RDS por API (no está en su matriz de servicios con precio)
- ``POST /rds/refresh``      consulta versiones y flavors de RDS a Huawei (ListDatastores/ListFlavors) [scans:run]
- ``GET  /bss-codes``        códigos de servicio/recurso/uso de BSS guardados (para RDS/OBS)
- ``POST /bss-codes/refresh``         consulta ListServiceTypes + ListResourceTypes        [scans:run]
- ``POST /bss-codes/usage/refresh``   consulta ListUsageTypes de un recurso                [scans:run]

Lectura: ``costs:read``. La consulta a Huawei exige ``scans:run``, usa las credenciales
cifradas de la cuenta y queda en la auditoría.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.authz import Permission, Principal
from core.crypto import SecretCipher
from costs import huawei_pricing
from core.errors import classify_exception
from costs.calculator import (
    MAX_ITEMS,
    MODES,
    OBS_CLASSES,
    CalcItem,
    item_from_payload,
    price_item,
    price_list,
    quotable,
)
from costs.huawei_pricing import RDS_ENGINES
from costs.catalog import PriceCatalog
from costs.configuration import DISK_TYPES, ConfigurationError
from costs.flavor_names import disco
from costs.region_comparison import project_for_region
from costs.region_simulation import flavor_dict
from db.session import get_db
from routers.cost_compare_api import MAX_QUOTES_PER_REFRESH, _clients, _regions
from routers.deps import get_cipher
from routers.security import get_principal, requires
from services.calculator_export import export_csv, export_pdf, export_xlsx
from tenancy import audit
from tenancy.accounts import get_account
from tenancy.errors import ValidationFailedError

router = APIRouter(prefix="/api/clients/{client_id}/accounts/{account_id}/calculator", tags=["calculator"],
                   dependencies=[Depends(requires(Permission.COSTS_READ))])
CALLS_HUAWEI = [Depends(requires(Permission.SCANS_RUN))]
EXPORT_TYPES = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "pdf": "application/pdf", "csv": "text/csv; charset=utf-8"}


class ItemsIn(BaseModel):
    items: List[Dict[str, Any]] = Field(..., min_length=1, max_length=MAX_ITEMS)


def _items(db: Session, payloads: List[Dict[str, Any]]) -> List[CalcItem]:
    known = [r.id for r in _regions(db)]
    try:
        return [item_from_payload(p, known) for p in payloads]
    except (ConfigurationError, ValueError) as exc:
        raise ValidationFailedError(str(exc)) from None


def _priced(db: Session, account_id: uuid.UUID, items: List[CalcItem]) -> List[Dict[str, Any]]:
    catalog = PriceCatalog(db)
    projects: Dict[str, bool] = {}
    out = []
    for item in items:
        if item.region not in projects:
            projects[item.region] = project_for_region(db, account_id, item.region) is not None
        out.append(price_item(catalog, item, has_project=projects[item.region]))
    return out


@router.get("/options")
def options(client_id: uuid.UUID, account_id: uuid.UUID, region: Optional[str] = Query(None, max_length=64),
            billing_mode: str = Query("monthly", pattern="^(monthly|yearly|on_demand)$"),
            os_type: str = Query("linux", pattern="^(linux|windows)$"),
            db: Session = Depends(get_db)) -> Dict[str, Any]:
    account = get_account(db, client_id, account_id)
    regions = _regions(db)
    with_project = {r.id: project_for_region(db, account.id, r.id) is not None for r in regions}
    result: Dict[str, Any] = {
        "regions": [{"id": r.id, "name": r.display_name, "has_project": with_project[r.id]} for r in regions],
        "modes": [{"id": k, **v} for k, v in MODES.items()],
        "disk_types": [{"id": d, "name": disco(d)} for d in DISK_TYPES],
        "max_items": MAX_ITEMS,
        "obs": {"classes": list(OBS_CLASSES)},
    }
    if region:
        if region not in with_project:
            raise ValidationFailedError("Región desconocida.")
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
            "region": region, "has_project": with_project[region], "flavors": flavors,
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
def quote(client_id: uuid.UUID, account_id: uuid.UUID, body: Dict[str, Any],
          db: Session = Depends(get_db)) -> Dict[str, Any]:
    account = get_account(db, client_id, account_id)
    item = _items(db, [body])[0]
    priced = _priced(db, account.id, [item])[0]
    return {k: v for k, v in priced.items() if not k.startswith("_")}


@router.post("/list")
def price_items(client_id: uuid.UUID, account_id: uuid.UUID, body: ItemsIn,
                db: Session = Depends(get_db)) -> Dict[str, Any]:
    account = get_account(db, client_id, account_id)
    return price_list(_priced(db, account.id, _items(db, body.items)))


@router.post("/flavors/refresh", dependencies=CALLS_HUAWEI)
def refresh_flavors(client_id: uuid.UUID, account_id: uuid.UUID, region: Optional[str] = Query(None, max_length=64),
                    db: Session = Depends(get_db), cipher: SecretCipher = Depends(get_cipher),
                    actor: Principal = Depends(get_principal)) -> Dict[str, Any]:
    """Trae los flavors ECS reales de Huawei Cloud para la región (o todas si no se especifica)."""
    account = get_account(db, client_id, account_id)
    regions = _regions(db)
    region_ids = {r.id: r for r in regions}
    if region:
        if region not in region_ids:
            raise ValidationFailedError("Región desconocida.")
        regions = [region_ids[region]]

    clients = _clients(db, cipher, client_id, account_id)
    store = PriceCatalog(db)
    stored, failures, now = {}, [], datetime.now(timezone.utc)

    for r in regions:
        project = project_for_region(db, account.id, r.id)
        if project is None:
            failures.append({"region": r.id, "reason": "Sin Project en la cuenta."})
            continue
        try:
            flavors = huawei_pricing.fetch_flavors(clients, region=r.id, project_id=project.huawei_project_id)
            count = store.replace_flavors(r.id, flavors, fetched_at=now)
            stored[r.id] = count
        except Exception as exc:
            error = classify_exception(exc, service="ecs", region=r.id, secrets=clients.secrets)
            failures.append({"region": r.id, "error": error.kind, "message": error.message,
                           "error_code": error.error_code})

    audit.record(db, actor, "catalog.refresh", client_id=client_id, account_id=account.id,
                 details={"regions": list(stored.keys()),
                         "fields": [f"flavors={sum(stored.values())}", f"errors={len(failures)}"]})
    return {"stored": stored, "failures": failures}


@router.post("/prices/refresh", dependencies=CALLS_HUAWEI)
def refresh_prices(client_id: uuid.UUID, account_id: uuid.UUID, body: ItemsIn, db: Session = Depends(get_db),
                   cipher: SecretCipher = Depends(get_cipher), actor: Principal = Depends(get_principal)):
    account = get_account(db, client_id, account_id)
    items = _items(db, body.items)
    wanted = quotable(items)
    if sum(len(c) for c in wanted.values()) > MAX_QUOTES_PER_REFRESH:
        raise ValidationFailedError(f"Demasiados componentes distintos para una consulta (máximo "
                                    f"{MAX_QUOTES_PER_REFRESH}).")
    clients = _clients(db, cipher, client_id, account_id)
    store = PriceCatalog(db)
    stored, failures = 0, []
    for (region, mode), components in sorted(wanted.items()):
        project = project_for_region(db, account.id, region)
        if project is None:
            failures += [{"component": c.label, "region": region,
                          "reason": "Sin Project en la cuenta: no se puede cotizar en esta región."} for c in components]
            continue
        quotes, failed = huawei_pricing.fetch_quotes(
            clients, project_id=project.huawei_project_id, region=region, components=components,
            billing_mode=mode, client_builder=huawei_pricing.default_bss_builder)
        stored += store.store(quotes)
        failures += failed
    audit.record(db, actor, "prices.refresh", client_id=client_id, account_id=account.id,
                 details={"regions": sorted({r for r, _ in wanted}),
                          "fields": ["mode=calculator", f"stored={stored}", f"failed={len(failures)}"]})
    return {"stored": stored, "failures": failures}


@router.post("/export")
def export(client_id: uuid.UUID, account_id: uuid.UUID, body: ItemsIn,
           format: str = Query("xlsx", pattern="^(xlsx|pdf|csv)$"), db: Session = Depends(get_db)) -> Response:
    """La lista se RECALCULA aquí con los precios oficiales guardados: nunca se usan importes del navegador."""
    account = get_account(db, client_id, account_id)
    priced = price_list(_priced(db, account.id, _items(db, body.items)))
    regions = {r.id: r.display_name for r in _regions(db)}
    stamp = datetime.now()
    builder = {"xlsx": export_xlsx, "pdf": export_pdf, "csv": export_csv}[format]
    content = builder(priced, regions=regions, generated_at=stamp)
    name = f"Calculadora_Huawei_{stamp:%Y%m%d_%H%M}.{format}"
    return Response(content=content, media_type=EXPORT_TYPES[format],
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- RDS
def _safe_error(exc: Exception, service: str, clients, region: Optional[str] = None) -> Dict[str, Any]:
    error = classify_exception(exc, service=service, region=region, secrets=clients.secrets)
    return {"servicio": service, "mensaje": error.message, "categoria": error.kind, "http_status": error.http_status,
            "error_code": error.error_code, "request_id": error.request_id, "accion_iam": error.iam_action}


def _engine(engine: str) -> str:
    if engine not in RDS_ENGINES:
        raise ValidationFailedError(f"Motor no válido ({', '.join(RDS_ENGINES)}).")
    return engine


@router.get("/rds/flavors")
def rds_flavors(client_id: uuid.UUID, account_id: uuid.UUID, region: str = Query(..., max_length=64),
                engine: str = Query(...), version: Optional[str] = Query(None, max_length=32),
                db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Catálogo informativo de flavors de RDS (sin precio: Huawei no lo cotiza por API)."""
    account = get_account(db, client_id, account_id)
    if region not in {r.id for r in _regions(db)}:
        raise ValidationFailedError("Región desconocida.")
    catalog = PriceCatalog(db)
    versions = catalog.rds_versions(region, _engine(engine))
    version = version if version in versions else (versions[0] if versions else None)
    rows = catalog.rds_flavors(region, engine, version) if version else []
    flavors = [{"spec_code": f.spec_code, "vcpus": f.vcpus, "ram_gb": float(str(f.ram_gb)),
                "instance_mode": f.instance_mode, "az_status": f.az_status or {},
                "available": any(v == "normal" for v in (f.az_status or {}).values()) or not f.az_status}
               for f in rows]
    return {"region": region, "engine": engine, "versions": versions, "version": version,
            "has_project": project_for_region(db, account.id, region) is not None, "flavors": flavors,
            "updated_at": max((f.fetched_at for f in rows), default=None),
            "pricing_supported": False,
            "note": "Catálogo informativo: Huawei Cloud no permite consultar precios de RDS por API."}


MAX_RDS_VERSIONS = 6


@router.post("/rds/refresh", dependencies=CALLS_HUAWEI)
def refresh_rds(client_id: uuid.UUID, account_id: uuid.UUID, region: str = Query(..., max_length=64),
                engine: str = Query(...), db: Session = Depends(get_db), cipher: SecretCipher = Depends(get_cipher),
                actor: Principal = Depends(get_principal)):
    if region not in {r.id for r in _regions(db)}:
        raise ValidationFailedError("Región desconocida.")
    engine = _engine(engine)
    account = get_account(db, client_id, account_id)
    project = project_for_region(db, account.id, region)
    if project is None:
        raise ValidationFailedError(f"Sin Project en la cuenta para {region}: no se puede consultar RDS.")
    clients = _clients(db, cipher, client_id, account_id)
    try:
        versions = huawei_pricing.fetch_rds_versions(clients, region=region, project_id=project.huawei_project_id,
                                                     engine=engine, client_builder=huawei_pricing.default_rds_builder)
    except Exception as exc:  # clasificado y redactado
        raise HTTPException(status_code=502, detail=_safe_error(exc, "rds", clients, region))
    store, now, stored, errors = PriceCatalog(db), datetime.now(timezone.utc), {}, []
    for version in versions[:MAX_RDS_VERSIONS]:
        try:
            flavors = huawei_pricing.fetch_rds_flavors(clients, region=region, project_id=project.huawei_project_id,
                                                       engine=engine, version=version,
                                                       client_builder=huawei_pricing.default_rds_builder)
            stored[version] = store.replace_rds_flavors(region, engine, version, flavors, fetched_at=now)
        except Exception as exc:
            errors.append(_safe_error(exc, "rds", clients, region))
    audit.record(db, actor, "catalog.refresh", client_id=client_id, account_id=account.id,
                 details={"region_id": region, "fields": [f"rds={engine}", f"versions={len(stored)}",
                                                          f"errors={len(errors)}"]})
    return {"region": region, "engine": engine, "versions": versions, "flavors": stored, "errors": errors}


# ---------------------------------------------------------------- códigos BSS
def _code_dict(row) -> Dict[str, Any]:
    return {"code": row.code, "name": row.name, "parent_code": row.parent_code}


@router.get("/bss-codes")
def bss_codes(client_id: uuid.UUID, account_id: uuid.UUID, search: Optional[str] = Query(None, max_length=40),
              service: Optional[str] = Query(None, max_length=128), resource: Optional[str] = Query(None, max_length=128),
              db: Session = Depends(get_db)) -> Dict[str, Any]:
    get_account(db, client_id, account_id)
    catalog = PriceCatalog(db)
    services = catalog.bss_codes("service")
    if search:
        needle = search.lower()
        services = [s for s in services if needle in s.code.lower() or needle in (s.name or "").lower()]
    return {"services": [_code_dict(s) for s in services[:200]],
            "resources": [_code_dict(r) for r in catalog.bss_codes("resource", service)] if service else [],
            "usages": [_code_dict(u) for u in catalog.bss_codes("usage", resource)] if resource else [],
            "updated_at": max((s.fetched_at for s in catalog.bss_codes("service")), default=None)}


@router.post("/bss-codes/refresh", dependencies=CALLS_HUAWEI)
def refresh_bss_codes(client_id: uuid.UUID, account_id: uuid.UUID, db: Session = Depends(get_db),
                      cipher: SecretCipher = Depends(get_cipher), actor: Principal = Depends(get_principal)):
    account = get_account(db, client_id, account_id)
    clients = _clients(db, cipher, client_id, account_id)
    try:
        codes = huawei_pricing.fetch_bss_codes(clients, client_builder=huawei_pricing.default_bss_builder)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc, "bss", clients))
    count = PriceCatalog(db).replace_bss_codes(codes, fetched_at=datetime.now(timezone.utc),
                                               kinds=("service", "resource"))
    audit.record(db, actor, "catalog.refresh", client_id=client_id, account_id=account.id,
                 details={"fields": ["bss_codes", f"codes={count}"]})
    return {"codes": count}


@router.post("/bss-codes/usage/refresh", dependencies=CALLS_HUAWEI)
def refresh_usage_codes(client_id: uuid.UUID, account_id: uuid.UUID, resource: str = Query(..., max_length=128),
                        db: Session = Depends(get_db), cipher: SecretCipher = Depends(get_cipher),
                        actor: Principal = Depends(get_principal)):
    account = get_account(db, client_id, account_id)
    catalog = PriceCatalog(db)
    if resource not in catalog.bss_code_set().get("resource", set()):
        raise ValidationFailedError("Recurso BSS desconocido: actualiza primero los códigos BSS.")
    clients = _clients(db, cipher, client_id, account_id)
    try:
        usages = huawei_pricing.fetch_usage_types(clients, resource, client_builder=huawei_pricing.default_bss_builder)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc, "bss", clients))
    count = catalog.replace_bss_codes(usages, fetched_at=datetime.now(timezone.utc), kinds=("usage",), parent=resource)
    audit.record(db, actor, "catalog.refresh", client_id=client_id, account_id=account.id,
                 details={"fields": ["bss_usage_codes", f"codes={count}"]})
    return {"resource": resource, "codes": count}
