# coding: utf-8
"""API interna multi-cliente: clientes, cuentas Huawei, proyectos e inventario por cuenta.

Incluye el router del motor de escaneo (``routers/scans.py``).

SEGURIDAD: todavía no hay login/RBAC (fase posterior). Por eso este router solo se
monta si ``INVENTORY_ADMIN_API=true`` y la app escucha únicamente en 127.0.0.1.
Ninguna respuesta incluye AK/SK ni ciphertext (los esquemas de salida no los tienen).
"""

from __future__ import annotations

import os
import uuid
from typing import List

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from core.catalog import SERVICIOS
from core.crypto import CryptoConfigurationError, SecretCipher, SecretDecryptionError
from db.session import DatabaseNotConfiguredError, get_db
from routers.common import install_safe_validation_errors, inventory_response
from routers.deps import _keyring_from_env, get_cipher  # noqa: F401  (reexportados)
from core.authz import Permission, Principal
from routers.security import get_principal, requires
from routers.audit_api import router as audit_api_router
from routers.cost_compare_api import router as cost_compare_api_router
from routers.costs_api import router as costs_api_router
from routers.exports_api import router as exports_api_router
from routers.inventory_api import router as inventory_api_router
from routers.schedules_api import router as schedules_api_router
from routers.scans import router as scans_router
from tenancy import accounts, audit, clients, projects
from tenancy.errors import TenancyError
from tenancy.inventory import run_account_inventory
from tenancy.projects import DiscoveryFailedError
from tenancy.schemas import (
    AccountIn,
    AccountInventoryIn,
    AccountOut,
    AccountPatch,
    ClientIn,
    ClientOut,
    ClientPatch,
    CredentialsIn,
    DiscoveryOut,
    ProjectIn,
    ProjectOut,
    ProjectPatch,
)

ENV_ADMIN_API = "INVENTORY_ADMIN_API"

router = APIRouter(prefix="/api/admin", tags=["admin"])
inventory_router = APIRouter(prefix="/api/clients", tags=["inventory"])


def admin_api_enabled() -> bool:
    return os.environ.get(ENV_ADMIN_API, "").strip().lower() in ("1", "true", "yes")


# ------------------------------------------------------------------ clientes
@router.get("/clients", response_model=List[ClientOut])
def list_clients(db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
    """Clientes visibles para quien llama (todos para administradores de plataforma)."""
    visible = principal.visible_client_ids()
    return [c for c in clients.list_clients(db) if visible is None or str(c.id) in visible]


@router.post("/clients", response_model=ClientOut, status_code=201, dependencies=[Depends(requires(Permission.CLIENTS_MANAGE))])
def create_client(body: ClientIn, db: Session = Depends(get_db), actor: Principal = Depends(get_principal)):
    client = clients.create_client(db, name=body.name, slug=body.slug, status=body.status)
    audit.record(db, actor, "client.create", client_id=client.id, target=("client", client.id),
                 details={"name": client.name, "slug": client.slug, "status": client.status})
    return client


@router.get("/clients/{client_id}", response_model=ClientOut, dependencies=[Depends(requires(Permission.CLIENTS_READ))])
def get_client(client_id: uuid.UUID, db: Session = Depends(get_db)):
    return clients.get_client(db, client_id)


@router.patch("/clients/{client_id}", response_model=ClientOut, dependencies=[Depends(requires(Permission.CLIENTS_MANAGE))])
def update_client(client_id: uuid.UUID, body: ClientPatch, db: Session = Depends(get_db), actor: Principal = Depends(get_principal)):
    changes = body.model_dump(exclude_unset=True)
    client = clients.update_client(db, client_id, **changes)
    audit.record(db, actor, "client.update", client_id=client_id, target=("client", client_id),
                 details={**changes, "fields": sorted(changes)})
    return client


@router.delete("/clients/{client_id}", status_code=204, dependencies=[Depends(requires(Permission.CLIENTS_MANAGE))])
def delete_client(client_id: uuid.UUID, db: Session = Depends(get_db), actor: Principal = Depends(get_principal)):
    name = clients.get_client(db, client_id).name
    clients.delete_client(db, client_id)
    audit.record(db, actor, "client.delete", client_id=client_id, target=("client", client_id),
                 details={"name": name})


# ------------------------------------------------------------------- cuentas
@router.get("/clients/{client_id}/accounts", response_model=List[AccountOut], dependencies=[Depends(requires(Permission.ACCOUNTS_READ))])
def list_accounts(client_id: uuid.UUID, db: Session = Depends(get_db)):
    return accounts.list_accounts(db, client_id)


@router.post("/clients/{client_id}/accounts", response_model=AccountOut, status_code=201, dependencies=[Depends(requires(Permission.ACCOUNTS_MANAGE))])
def create_account(client_id: uuid.UUID, body: AccountIn, db: Session = Depends(get_db),
                   cipher: SecretCipher = Depends(get_cipher), actor: Principal = Depends(get_principal)):
    data = body.model_dump(exclude={"ak", "sk"})
    account = accounts.create_account(db, cipher, client_id=client_id, ak=body.ak.get_secret_value(),
                                      sk=body.sk.get_secret_value(), **data)
    audit.record(db, actor, "account.create", client_id=client_id, account_id=account.id,
                 target=("account", account.id),
                 details={"name": account.name, "endpoint_domain": account.endpoint_domain,
                          "key_version": account.key_version})
    return account


@router.get("/clients/{client_id}/accounts/{account_id}", response_model=AccountOut, dependencies=[Depends(requires(Permission.ACCOUNTS_READ))])
def get_account(client_id: uuid.UUID, account_id: uuid.UUID, db: Session = Depends(get_db)):
    return accounts.get_account(db, client_id, account_id)


@router.patch("/clients/{client_id}/accounts/{account_id}", response_model=AccountOut, dependencies=[Depends(requires(Permission.ACCOUNTS_MANAGE))])
def update_account(client_id: uuid.UUID, account_id: uuid.UUID, body: AccountPatch,
                   db: Session = Depends(get_db), actor: Principal = Depends(get_principal)):
    changes = body.model_dump(exclude_unset=True)
    account = accounts.update_account(db, client_id, account_id, **changes)
    audit.record(db, actor, "account.update", client_id=client_id, account_id=account_id,
                 target=("account", account_id), details={**changes, "fields": sorted(changes)})
    return account


@router.put("/clients/{client_id}/accounts/{account_id}/credentials", response_model=AccountOut, dependencies=[Depends(requires(Permission.CREDENTIALS_MANAGE))])
def replace_credentials(client_id: uuid.UUID, account_id: uuid.UUID, body: CredentialsIn,
                        db: Session = Depends(get_db), cipher: SecretCipher = Depends(get_cipher), actor: Principal = Depends(get_principal)):
    account = accounts.replace_credentials(db, cipher, client_id, account_id,
                                           ak=body.ak.get_secret_value(), sk=body.sk.get_secret_value())
    # Solo se registra el hecho y la versión de clave: nunca los valores.
    audit.record(db, actor, "account.credentials.replace", client_id=client_id, account_id=account_id,
                 target=("account", account_id), details={"key_version": account.key_version})
    return account


@router.delete("/clients/{client_id}/accounts/{account_id}", status_code=204, dependencies=[Depends(requires(Permission.ACCOUNTS_MANAGE))])
def delete_account(client_id: uuid.UUID, account_id: uuid.UUID, db: Session = Depends(get_db), actor: Principal = Depends(get_principal)):
    name = accounts.get_account(db, client_id, account_id).name
    accounts.delete_account(db, client_id, account_id)
    audit.record(db, actor, "account.delete", client_id=client_id, account_id=account_id,
                 target=("account", account_id), details={"name": name})


# ----------------------------------------------------------------- proyectos
@router.get("/clients/{client_id}/accounts/{account_id}/projects", response_model=List[ProjectOut], dependencies=[Depends(requires(Permission.ACCOUNTS_READ))])
def list_projects(client_id: uuid.UUID, account_id: uuid.UUID, db: Session = Depends(get_db)):
    return projects.list_projects(db, client_id, account_id)


@router.post("/clients/{client_id}/accounts/{account_id}/projects", response_model=ProjectOut,
             status_code=201, dependencies=[Depends(requires(Permission.PROJECTS_MANAGE))])
def add_project(client_id: uuid.UUID, account_id: uuid.UUID, body: ProjectIn, db: Session = Depends(get_db),
                actor: Principal = Depends(get_principal)):
    project = projects.add_project(db, client_id, account_id, **body.model_dump())
    audit.record(db, actor, "project.add", client_id=client_id, account_id=account_id,
                 target=("project", project.id),
                 details={"huawei_project_id": project.huawei_project_id, "region_id": project.region_id})
    return project


@router.patch("/clients/{client_id}/accounts/{account_id}/projects/{project_id}", response_model=ProjectOut, dependencies=[Depends(requires(Permission.PROJECTS_MANAGE))])
def update_project(client_id: uuid.UUID, account_id: uuid.UUID, project_id: uuid.UUID,
                   body: ProjectPatch, db: Session = Depends(get_db), actor: Principal = Depends(get_principal)):
    project = projects.set_project_enabled(db, client_id, account_id, project_id, body.is_enabled)
    audit.record(db, actor, "project.update", client_id=client_id, account_id=account_id,
                 target=("project", project_id), details={"is_enabled": body.is_enabled})
    return project


@router.post("/clients/{client_id}/accounts/{account_id}/discover-projects", response_model=DiscoveryOut, dependencies=[Depends(requires(Permission.PROJECTS_MANAGE))])
def discover_projects(client_id: uuid.UUID, account_id: uuid.UUID, db: Session = Depends(get_db),
                      cipher: SecretCipher = Depends(get_cipher), actor: Principal = Depends(get_principal)):
    try:
        result = projects.discover_projects(db, cipher, client_id, account_id)
    except DiscoveryFailedError as exc:
        audit.record(db, actor, "account.discover", client_id=client_id, account_id=account_id,
                     target=("account", account_id), details={"status": "failed"})
        db.commit()  # conserva status/last_validation_error de la cuenta y la auditoría
        raise HTTPException(status_code=502, detail={
            "mensaje": exc.error.message, "http_status": exc.error.http_status,
            "error_code": exc.error.error_code, "request_id": exc.error.request_id,
        })
    summary = result.summary()
    audit.record(db, actor, "account.discover", client_id=client_id, account_id=account_id,
                 target=("account", account_id),
                 details={"status": "ok", "created": summary["created"], "updated": summary["updated"],
                          "skipped": len(summary["skipped"])})
    return DiscoveryOut(**summary, projects=[ProjectOut.model_validate(p) for p in
                                                       projects.list_projects(db, client_id, account_id)])


# ------------------------------------------------- inventario por cuenta
@inventory_router.post("/{client_id}/accounts/{account_id}/inventory",
                       dependencies=[Depends(requires(Permission.SCANS_RUN))])
def account_inventory(client_id: uuid.UUID, account_id: uuid.UUID, body: AccountInventoryIn,
                      db: Session = Depends(get_db), cipher: SecretCipher = Depends(get_cipher)):
    """Mismo formato de respuesta que ``/api/inventory``, sin AK/SK en la petición."""
    service = body.service.strip().lower()
    if service not in {s["id"] for s in SERVICIOS}:
        raise HTTPException(status_code=400, detail="Servicio no válido")
    result = run_account_inventory(db, cipher, client_id=client_id, account_id=account_id,
                                   project_id=body.project_id, service=service)
    return inventory_response(service, result.region, result.huawei_project_id, result.resultado)


# ------------------------------------------------------------ instalación
def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": message})


def install_admin_api(app: FastAPI) -> None:
    """Monta los routers y traduce errores de dominio/configuración a HTTP seguros."""
    install_safe_validation_errors(app)
    protected = [Depends(get_principal)]  # token o solo local (ver routers/security.py)
    app.include_router(router, dependencies=protected)
    app.include_router(inventory_router, dependencies=protected)
    app.include_router(scans_router, dependencies=protected)
    app.include_router(inventory_api_router, dependencies=protected)
    app.include_router(exports_api_router, dependencies=protected)
    app.include_router(schedules_api_router, dependencies=protected)
    app.include_router(costs_api_router, dependencies=protected)
    app.include_router(cost_compare_api_router, dependencies=protected)
    app.include_router(audit_api_router, dependencies=protected)

    @app.exception_handler(TenancyError)
    async def _tenancy(request: Request, exc: TenancyError):
        return _error(exc.status_code, exc.message)

    @app.exception_handler(DatabaseNotConfiguredError)
    async def _no_db(request: Request, exc: DatabaseNotConfiguredError):
        return _error(503, "Base de datos no configurada (DATABASE_URL).")

    @app.exception_handler(CryptoConfigurationError)
    async def _no_keys(request: Request, exc: CryptoConfigurationError):
        return _error(503, "Cifrado no configurado (INVENTORY_ENCRYPTION_KEYS).")

    @app.exception_handler(SecretDecryptionError)
    async def _decrypt(request: Request, exc: SecretDecryptionError):
        return _error(500, "No se pudieron descifrar las credenciales de la cuenta.")
