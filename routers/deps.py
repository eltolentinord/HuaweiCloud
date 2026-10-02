# coding: utf-8
"""Dependencias FastAPI compartidas por los routers internos."""

from __future__ import annotations

from functools import lru_cache

from core.crypto import FernetKeyring, SecretCipher


@lru_cache(maxsize=1)
def _keyring_from_env() -> FernetKeyring:
    return FernetKeyring.from_env()


def get_cipher() -> SecretCipher:
    """Cifrado de credenciales (sustituible en tests con ``app.dependency_overrides``)."""
    return _keyring_from_env()
