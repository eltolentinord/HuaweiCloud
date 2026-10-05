# coding: utf-8
"""Alta, edición y baja de servidores (por cliente) con la contraseña CIFRADA.

La contraseña (SSH y sudo) se cifra con ``SecretCipher`` (contexto ``server:<id>:password``,
el mismo esquema que las AK/SK) y NUNCA se devuelve, registra ni audita en claro.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import uuid
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.crypto import SecretCipher
from db.models import Server
from tenancy.clients import get_client
from tenancy.errors import ConflictError, NotFoundError, ValidationFailedError

logger = logging.getLogger(__name__)
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
MAX_PASSWORD = 512


def _context(server_id: uuid.UUID) -> str:
    return f"server:{server_id}:password"


def validate_host(host: str) -> str:
    host = (host or "").strip()
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if not HOSTNAME_RE.match(host):
        raise ValidationFailedError("Host no válido: usa una IP o un nombre DNS (sin http:// ni espacios).")
    return host.lower()


def _validate(name: str, host: str, port: int, username: str):
    name = (name or "").strip()
    if not 1 <= len(name) <= 100:
        raise ValidationFailedError("El nombre es obligatorio (máximo 100 caracteres).")
    if not 1 <= int(port) <= 65535:
        raise ValidationFailedError("Puerto no válido (1-65535).")
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise ValidationFailedError("Usuario no válido (letras minúsculas, números, '_', '.', '-').")
    return name, validate_host(host), int(port), username


def _check_password(password: Optional[str]) -> str:
    if not password or len(password) > MAX_PASSWORD:
        raise ValidationFailedError("La contraseña es obligatoria.")
    return password


def get_server(session: Session, client_id: uuid.UUID, server_id: uuid.UUID) -> Server:
    server = session.scalars(select(Server).where(Server.client_id == client_id, Server.id == server_id)).first()
    if server is None:
        raise NotFoundError("Servidor no encontrado.")
    return server


def list_servers(session: Session, client_id: uuid.UUID) -> List[Server]:
    get_client(session, client_id)
    return list(session.scalars(select(Server).where(Server.client_id == client_id).order_by(Server.name)))


def create_server(session: Session, cipher: SecretCipher, *, client_id: uuid.UUID, name: str, host: str,
                  port: int, username: str, password: str, description: Optional[str] = None) -> Server:
    get_client(session, client_id)
    name, host, port, username = _validate(name, host, port, username)
    server_id = uuid.uuid4()
    token, version = cipher.encrypt(_check_password(password), context=_context(server_id))
    server = Server(id=server_id, client_id=client_id, name=name, host=host, port=port, username=username,
                    password_ciphertext=token, key_version=version,
                    description=(description or "").strip()[:500] or None)
    session.add(server)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise ConflictError("Ya existe un servidor con ese nombre en este cliente.") from None
    logger.info("Servidor creado id=%s client=%s", server.id, client_id)
    return server


def update_server(session: Session, cipher: SecretCipher, server: Server, *, name: str, host: str, port: int,
                  username: str, password: Optional[str] = None, description: Optional[str] = None,
                  enabled: bool = True) -> Server:
    name, host, port, username = _validate(name, host, port, username)
    if (host, port) != (server.host, server.port):
        server.host_fingerprint = None   # otro equipo: la huella se vuelve a registrar en la próxima conexión
    server.name, server.host, server.port, server.username = name, host, port, username
    server.description = (description or "").strip()[:500] or None
    server.enabled = bool(enabled)
    if password:
        replace_password(session, cipher, server, password)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise ConflictError("Ya existe un servidor con ese nombre en este cliente.") from None
    return server


def replace_password(session: Session, cipher: SecretCipher, server: Server, password: str) -> None:
    token, version = cipher.encrypt(_check_password(password), context=_context(server.id))
    server.password_ciphertext, server.key_version = token, version
    session.flush()
    logger.info("Contraseña de servidor reemplazada id=%s key_version=%s", server.id, version)


def decrypt_password(server: Server, cipher: SecretCipher) -> str:
    return cipher.decrypt(server.password_ciphertext, key_version=server.key_version, context=_context(server.id))


def reencrypt_servers(session: Session, cipher: SecretCipher) -> int:
    """Recifra con la clave actual (rotación de INVENTORY_ENCRYPTION_KEYS)."""
    count = 0
    for server in session.scalars(select(Server).where(Server.key_version != cipher.current_version)):
        replace_password(session, cipher, server, decrypt_password(server, cipher))
        count += 1
    return count
