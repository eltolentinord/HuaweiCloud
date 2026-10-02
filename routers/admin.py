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
from routers.inventory_api import router as inventory_api_router
from routers.scans import router as scans_router
from tenancy import accounts, clients, projects
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
def list_clients(db: Session = Depends(get_db)):
    return clients.list_clients(db)


@router.post("/clients", response_model=ClientOut, status_code=201)
def create_client(body: ClientIn, db: Session = Depends(get_db)):
    return clients.create_client(db, name=body.name, slug=body.slug, status=body.status)


@router.get("/clients/{client_id}", response_model=ClientOut)
def get_client(client_id: uuid.UUID, db: Session = Depends(get_db)):
    return clients.get_client(db, client_id)


@router.patch("/clients/{client_id}", response_model=ClientOut)
def update_client(client_id: uuid.UUID, body: ClientPatch, db: Session = Depends(get_db)):
    return clients.update_client(db, client_id, **body.model_dump(exclude_unset=True))


@router.delete("/clients/{client_id}", status_code=204)
def delete_client(client_id: uuid.UUID, db: Session = Depends(get_db)):
    clients.delete_client(db, client_id)


# ------------------------------------------------------------------- cuentas
@router.get("/clients/{client_id}/accounts", response_model=List[AccountOut])
def list_accounts(client_id: uuid.UUID, db: Session = Depends(get_db)):
    return accounts.list_accounts(db, client_id)


@router.post("/clients/{client_id}/accounts", response_model=AccountOut, status_code=201)
def create_account(client_id: uuid.UUID, body: AccountIn, db: Session = Depends(get_db),
                   cipher: SecretCipher = Depends(get_cipher)):
    data = body.model_dump(exclude={"ak", "sk"})
    return accounts.create_account(db, cipher, client_id=client_id, ak=body.ak.get_secret_value(),
                                   sk=body.sk.get_secret_value(), **data)


@router.get("/clients/{client_id}/accounts/{account_id}", response_model=AccountOut)
def get_account(client_id: uuid.UUID, account_id: uuid.UUID, db: Session = Depends(get_db)):
    return accounts.get_account(db, client_id, account_id)


@router.patch("/clients/{client_id}/accounts/{account_id}", response_model=AccountOut)
def update_account(client_id: uuid.UUID, account_id: uuid.UUID, body: AccountPatch,
                   db: Session = Depends(get_db)):
    return accounts.update_account(db, client_id, account_id, **body.model_dump(exclude_unset=True))


@router.put("/clients/{client_id}/accounts/{account_id}/credentials", response_model=AccountOut)
def replace_credentials(client_id: uuid.UUID, account_id: uuid.UUID, body: CredentialsIn,
                        db: Session = Depends(get_db), cipher: SecretCipher = Depends(get_cipher)):
    return accounts.replace_credentials(db, cipher, client_id, account_id,
                                        ak=body.ak.get_secret_value(), sk=body.sk.get_secret_value())


@router.delete("/clients/{client_id}/accounts/{account_id}", status_code=204)
def delete_account(client_id: uuid.UUID, account_id: uuid.UUID, db: Session = Depends(get_db)):
    accounts.delete_account(db, client_id, account_id)


# ----------------------------------------------------------------- proyectos
@router.get("/clients/{client_id}/accounts/{account_id}/projects", response_model=List[ProjectOut])
def list_projects(client_id: uuid.UUID, account_id: uuid.UUID, db: Session = Depends(get_db)):
    return projects.list_projects(db, client_id, account_id)


@router.post("/clients/{client_id}/accounts/{account_id}/projects", response_model=ProjectOut,
             status_code=201)
def add_project(client_id: uuid.UUID, account_id: uuid.UUID, body: ProjectIn, db: Session = Depends(get_db)):
    return projects.add_project(db, client_id, account_id, **body.model_dump())


@router.patch("/clients/{client_id}/accounts/{account_id}/projects/{project_id}", response_model=ProjectOut)
def update_project(client_id: uuid.UUID, account_id: uuid.UUID, project_id: uuid.UUID,
                   body: ProjectPatch, db: Session = Depends(get_db)):
    return projects.set_project_enabled(db, client_id, account_id, project_id, body.is_enabled)


@router.post("/clients/{client_id}/accounts/{account_id}/discover-projects", response_model=DiscoveryOut)
def discover_projects(client_id: uuid.UUID, account_id: uuid.UUID, db: Session = Depends(get_db),
                      cipher: SecretCipher = Depends(get_cipher)):
    try:
        result = projects.discover_projects(db, cipher, client_id, account_id)
    except DiscoveryFailedError as exc:
        db.commit()  # conserva status/last_validation_error de la cuenta
        raise HTTPException(status_code=502, detail={
            "mensaje": exc.error.message, "http_status": exc.error.http_status,
            "error_code": exc.error.error_code, "request_id": exc.error.request_id,
        })
    return DiscoveryOut(**result.summary(), projects=projects.list_projects(db, client_id, account_id))


# ------------------------------------------------- inventario por cuenta
@inventory_router.post("/{client_id}/accounts/{account_id}/inventory")
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
    app.include_router(router)
    app.include_router(inventory_router)
    app.include_router(scans_router)
    app.include_router(inventory_api_router)

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
