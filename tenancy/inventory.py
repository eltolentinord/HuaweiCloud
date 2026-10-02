# coding: utf-8
"""Inventario a partir de una ``cloud_account_id``: el navegador no envía AK/SK.

Flujo: cliente → cuenta → proyecto (región) → DatabaseCredentialProvider →
collectors → Huawei Cloud.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict

from sqlalchemy.orm import Session

from core.crypto import SecretCipher
from inventory import consultar_con_proveedor
from tenancy.accounts import DatabaseCredentialProvider, get_account
from tenancy.errors import InvalidStateError
from tenancy.projects import get_project


@dataclass(frozen=True)
class AccountInventoryResult:
    region: str
    huawei_project_id: str
    resultado: Dict[str, Any]


def run_account_inventory(session: Session, cipher: SecretCipher, *, client_id: uuid.UUID,
                          account_id: uuid.UUID, project_id: uuid.UUID,
                          service: str) -> AccountInventoryResult:
    """Ejecuta el inventario de ``service`` (o "todos") en un proyecto de la cuenta."""
    account = get_account(session, client_id, account_id)
    if account.status in ("disabled", "invalid"):
        raise InvalidStateError(f"La cuenta está en estado '{account.status}'.")
    project = get_project(session, client_id, account_id, project_id)
    if not project.is_enabled:
        raise InvalidStateError("El proyecto está deshabilitado.")

    provider = DatabaseCredentialProvider(session, cipher, client_id=client_id, account_id=account_id)
    resultado = consultar_con_proveedor(service, provider, project.huawei_project_id, project.region_id,
                                        endpoint_domain=account.endpoint_domain)
    return AccountInventoryResult(region=project.region_id, huawei_project_id=project.huawei_project_id,
                                  resultado=resultado)
