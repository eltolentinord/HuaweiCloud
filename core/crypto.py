# coding: utf-8
"""Cifrado autenticado de secretos (AK/SK) con claves maestras versionadas.

Proveedor actual: Fernet (AES-128-CBC + HMAC-SHA256, de ``cryptography``).
Las claves maestras se leen EXCLUSIVAMENTE de variables de entorno:

    INVENTORY_ENCRYPTION_KEYS="1:<clave_fernet_base64>,2:<otra_clave>"
    INVENTORY_ENCRYPTION_KEY_VERSION=2      # opcional; por defecto la mayor

- Cada texto cifrado se guarda con su ``key_version`` → rotación sin downtime:
  se añade la clave nueva, se recifran las cuentas y se retira la antigua.
- Cada secreto se liga a un CONTEXTO (p. ej. ``cloud_account:<id>:ak``): un
  ciphertext copiado a otra cuenta o a otro campo no descifra.
- ``SecretCipher`` es el protocolo a implementar para sustituir Fernet por un KMS.

Ningún mensaje de error de este módulo incluye texto plano ni claves.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Protocol, Tuple

from cryptography.fernet import Fernet, InvalidToken

ENV_KEYS = "INVENTORY_ENCRYPTION_KEYS"
ENV_KEY_VERSION = "INVENTORY_ENCRYPTION_KEY_VERSION"
_SEPARATOR = "\x1f"  # separa contexto y secreto dentro del texto cifrado


class CryptoConfigurationError(RuntimeError):
    """Claves maestras ausentes o mal formadas."""


class SecretDecryptionError(RuntimeError):
    """El secreto no se pudo descifrar (clave incorrecta, versión retirada o datos alterados)."""


class SecretCipher(Protocol):
    """Contrato de cifrado de secretos (Fernet hoy; KMS en el futuro)."""

    @property
    def current_version(self) -> int:
        ...

    def encrypt(self, plaintext: str, *, context: str) -> Tuple[bytes, int]:
        """Devuelve ``(ciphertext, key_version)``."""

    def decrypt(self, ciphertext: bytes, *, key_version: int, context: str) -> str:
        ...


def generate_key() -> str:
    """Nueva clave Fernet (base64 url-safe, 32 bytes)."""
    return Fernet.generate_key().decode("ascii")


@dataclass(frozen=True)
class FernetKeyring:
    """Anillo de claves Fernet indexadas por versión."""

    keys: Mapping[int, Fernet]
    current: int

    def __post_init__(self) -> None:
        if self.current not in self.keys:
            raise CryptoConfigurationError(f"No existe la clave de versión {self.current}.")

    def __repr__(self) -> str:
        return f"FernetKeyring(versions={sorted(self.keys)}, current={self.current})"

    @property
    def current_version(self) -> int:
        return self.current

    @classmethod
    def from_keys(cls, keys: Mapping[int, str], current: Optional[int] = None) -> "FernetKeyring":
        if not keys:
            raise CryptoConfigurationError("No hay claves de cifrado configuradas.")
        try:
            parsed: Dict[int, Fernet] = {int(v): Fernet(k.encode("ascii")) for v, k in keys.items()}
        except (ValueError, TypeError):
            raise CryptoConfigurationError("Una clave de cifrado no es una clave Fernet válida.") from None
        if any(v <= 0 for v in parsed):
            raise CryptoConfigurationError("Las versiones de clave deben ser enteros positivos.")
        return cls(keys=parsed, current=current if current is not None else max(parsed))

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "FernetKeyring":
        environ = os.environ if environ is None else environ
        raw = (environ.get(ENV_KEYS) or "").strip()
        if not raw:
            raise CryptoConfigurationError(
                f"Define {ENV_KEYS} (ver .env.example). Genera una clave con: python manage.py keys generate"
            )
        keys: Dict[int, str] = {}
        for entry in filter(None, (e.strip() for e in raw.split(","))):
            version, sep, key = entry.partition(":")
            if not sep or not version.strip().isdigit():
                raise CryptoConfigurationError(f"Formato de {ENV_KEYS} inválido; usa 'version:clave'.")
            keys[int(version)] = key.strip()
        current = (environ.get(ENV_KEY_VERSION) or "").strip()
        return cls.from_keys(keys, int(current) if current.isdigit() else None)

    def encrypt(self, plaintext: str, *, context: str) -> Tuple[bytes, int]:
        if not plaintext:
            raise ValueError("No se puede cifrar un secreto vacío.")
        payload = f"{context}{_SEPARATOR}{plaintext}".encode("utf-8")
        return self.keys[self.current].encrypt(payload), self.current

    def decrypt(self, ciphertext: bytes, *, key_version: int, context: str) -> str:
        fernet = self.keys.get(key_version)
        if fernet is None:
            raise SecretDecryptionError(f"La clave de versión {key_version} no está disponible.")
        try:
            payload = fernet.decrypt(bytes(ciphertext)).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError):
            raise SecretDecryptionError("El secreto cifrado no es válido para la clave configurada.") from None
        stored_context, sep, plaintext = payload.partition(_SEPARATOR)
        if not sep or stored_context != context:
            raise SecretDecryptionError("El secreto cifrado no pertenece a este registro.")
        return plaintext

    def reencrypt(self, ciphertext: bytes, *, key_version: int, context: str) -> Tuple[bytes, int]:
        """Recifra con la clave actual (rotación)."""
        return self.encrypt(self.decrypt(ciphertext, key_version=key_version, context=context),
                            context=context)
