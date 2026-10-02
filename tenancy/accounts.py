# coding: utf-8
"""Cuentas Huawei Cloud de un cliente con AK/SK cifradas.

Reglas:
- AK/SK solo existen en claro dentro de la llamada que las cifra o descifra.
- Nunca se devuelven, se registran en logs ni se incluyen en excepciones.
- Todas las búsquedas exigen ``client_id`` (aislamiento entre clientes).
"""

from __future__ import annotations

import logging
import uuid
from typing import List, Optional

from sqlalchemy.orm import Session

from core.credentials import HuaweiCredentials, MissingCredentialsError
from core.crypto import SecretCipher
from core.validation import InvalidValueError, validate_endpoint_domain, validate_region_id
from db.models import ACCOUNT_STATUSES, DEFAULT_ENDPOINT_DOMAIN, CloudAccount
from repositories import accounts as repo
from tenancy.clients import get_client
from tenancy.errors import ConflictError, InvalidStateError, NotFoundError, ValidationFailedError

logger = logging.getLogger(__name__)

AK_FIELD = "ak"
SK_FIELD = "sk"


def credential_context(account_id: uuid.UUID, field: str) -> str:
    """Contexto ligado al ciphertext: impide reutilizarlo en otra cuenta/campo."""
    return f"cloud_account:{account_id}:{field}"


def _validated_location(endpoint_domain: Optional[str], iam_region_id: Optional[str]):
    """Dominio de endpoint en lista blanca y región con formato válido (anti-SSRF)."""
    try:
        domain = validate_endpoint_domain(endpoint_domain or DEFAULT_ENDPOINT_DOMAIN)
        region = validate_region_id(iam_region_id) if iam_region_id else None
    except InvalidValueError as exc:
        raise ValidationFailedError(str(exc)) from None
    return domain, region


def _validated_credentials(ak: str, sk: str) -> HuaweiCredentials:
    try:
        return HuaweiCredentials(ak=ak, sk=sk)
    except MissingCredentialsError:
        raise ValidationFailedError("AK y SK son obligatorios.") from None


def _store_credentials(account: CloudAccount, credentials: HuaweiCredentials,
                       cipher: SecretCipher) -> None:
    ak_token, version = cipher.encrypt(credentials.ak, context=credential_context(account.id, AK_FIELD))
    sk_token, sk_version = cipher.encrypt(credentials.sk, context=credential_context(account.id, SK_FIELD))
    if version != sk_version:  # la clave actual no puede cambiar entre dos llamadas
        raise RuntimeError("Versión de clave inconsistente al cifrar credenciales.")
    account.ak_ciphertext, account.sk_ciphertext, account.key_version = ak_token, sk_token, version


def _ensure_unique_name(session: Session, client_id: uuid.UUID, name: str,
                        exclude: Optional[uuid.UUID] = None) -> None:
    existing = repo.get_by_name(session, client_id, name)
    if existing is not None and existing.id != exclude:
        raise ConflictError(f"El cliente ya tiene una cuenta llamada '{name}'.")


def create_account(
    session: Session,
    cipher: SecretCipher,
    *,
    client_id: uuid.UUID,
    name: str,
    ak: str,
    sk: str,
    huawei_domain_id: Optional[str] = None,
    huawei_domain_name: Optional[str] = None,
    endpoint_domain: str = DEFAULT_ENDPOINT_DOMAIN,
    iam_region_id: Optional[str] = None,
) -> CloudAccount:
    get_client(session, client_id)
    if not name or not name.strip():
        raise ValidationFailedError("El nombre de la cuenta es obligatorio.")
    endpoint_domain, iam_region_id = _validated_location(endpoint_domain, iam_region_id)
    credentials = _validated_credentials(ak, sk)
    _ensure_unique_name(session, client_id, name.strip())
    account = CloudAccount(
        id=uuid.uuid4(),
        client_id=client_id,
        name=name.strip(),
        huawei_domain_id=huawei_domain_id or None,
        huawei_domain_name=huawei_domain_name or None,
        endpoint_domain=endpoint_domain,
        iam_region_id=iam_region_id,
        status="pending",
    )
    _store_credentials(account, credentials, cipher)
    session.add(account)
    session.flush()
    logger.info("Cuenta creada id=%s client=%s key_version=%s", account.id, client_id, account.key_version)
    return account


def get_account(session: Session, client_id: uuid.UUID, account_id: uuid.UUID) -> CloudAccount:
    account = repo.get_for_client(session, client_id, account_id)
    if account is None:
        raise NotFoundError("Cuenta no encontrada.")
    return account


def list_accounts(session: Session, client_id: uuid.UUID) -> List[CloudAccount]:
    get_client(session, client_id)
    return repo.list_for_client(session, client_id)


def update_account(session: Session, client_id: uuid.UUID, account_id: uuid.UUID, *,
                   name: Optional[str] = None, status: Optional[str] = None,
                   huawei_domain_id: Optional[str] = None, huawei_domain_name: Optional[str] = None,
                   endpoint_domain: Optional[str] = None, iam_region_id: Optional[str] = None) -> CloudAccount:
    account = get_account(session, client_id, account_id)
    if name is not None:
        if not name.strip():
            raise ValidationFailedError("El nombre de la cuenta es obligatorio.")
        _ensure_unique_name(session, client_id, name.strip(), exclude=account.id)
        account.name = name.strip()
    if status is not None:
        if status not in ACCOUNT_STATUSES:
            raise ValidationFailedError(f"Estado inválido; usa uno de {', '.join(ACCOUNT_STATUSES)}.")
        account.status = status
    for field, value in (("huawei_domain_id", huawei_domain_id), ("huawei_domain_name", huawei_domain_name)):
        if value is not None:
            setattr(account, field, value or None)  # "" borra el valor
    if iam_region_id is not None:
        account.iam_region_id = _validated_location(account.endpoint_domain, iam_region_id)[1]
    if endpoint_domain is not None:
        account.endpoint_domain = _validated_location(endpoint_domain, None)[0]
    session.flush()
    return account


def replace_credentials(session: Session, cipher: SecretCipher, client_id: uuid.UUID,
                        account_id: uuid.UUID, *, ak: str, sk: str) -> CloudAccount:
    """Sustituye AK/SK (p. ej. tras rotarlas en IAM). La cuenta vuelve a 'pending'."""
    account = get_account(session, client_id, account_id)
    _store_credentials(account, _validated_credentials(ak, sk), cipher)
    account.status = "pending"
    account.last_validated_at = None
    account.last_validation_error = None
    session.flush()
    logger.info("Credenciales reemplazadas id=%s key_version=%s", account.id, account.key_version)
    return account


def delete_account(session: Session, client_id: uuid.UUID, account_id: uuid.UUID) -> None:
    session.delete(get_account(session, client_id, account_id))
    session.flush()


def _decrypt(account: CloudAccount, cipher: SecretCipher) -> HuaweiCredentials:
    ak = cipher.decrypt(account.ak_ciphertext, key_version=account.key_version,
                        context=credential_context(account.id, AK_FIELD))
    sk = cipher.decrypt(account.sk_ciphertext, key_version=account.key_version,
                        context=credential_context(account.id, SK_FIELD))
    return HuaweiCredentials(ak=ak, sk=sk)


def decrypt_credentials(account: CloudAccount, cipher: SecretCipher) -> HuaweiCredentials:
    """Única vía para obtener AK/SK en claro desde la base de datos."""
    if account.status == "disabled":
        raise InvalidStateError("La cuenta está deshabilitada.")
    return _decrypt(account, cipher)


def reencrypt_all(session: Session, cipher: SecretCipher) -> int:
    """Recifra con la clave actual las cuentas que usan versiones anteriores."""
    count = 0
    for account in repo.list_with_other_key_version(session, cipher.current_version):
        credentials = _decrypt(account, cipher)
        _store_credentials(account, credentials, cipher)
        count += 1
    session.flush()
    logger.info("Cuentas recifradas: %d (key_version=%s)", count, cipher.current_version)
    return count


class DatabaseCredentialProvider:
    """``CredentialProvider`` respaldado por PostgreSQL (implementa el protocolo de core)."""

    def __init__(self, session: Session, cipher: SecretCipher, *, client_id: uuid.UUID,
                 account_id: uuid.UUID) -> None:
        self._session = session
        self._cipher = cipher
        self._client_id = client_id
        self._account_id = account_id

    def __repr__(self) -> str:
        return f"DatabaseCredentialProvider(account_id={self._account_id})"

    def get_credentials(self) -> HuaweiCredentials:
        account = get_account(self._session, self._client_id, self._account_id)
        return decrypt_credentials(account, self._cipher)
