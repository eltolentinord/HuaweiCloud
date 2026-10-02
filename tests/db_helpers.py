# coding: utf-8
"""Infraestructura de tests de base de datos.

Estrategia:
- Modelos, servicios y API se prueban en SQLite en memoria (siempre disponible,
  mismas constraints/FK gracias a ``PRAGMA foreign_keys``).
- Migraciones y almacenamiento real (BYTEA, constraints de PostgreSQL) se prueban
  contra PostgreSQL REAL: ``TEST_DATABASE_URL`` si está definida; si no, un
  PostgreSQL embebido con ``pgserver`` (requirements-dev.txt). Si ninguno está
  disponible esos tests se marcan como SKIPPED (nunca pasan en falso).
"""

from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from functools import lru_cache
from typing import Optional

from tests.helpers import ROOT  # noqa: F401  (asegura sys.path)

from sqlalchemy.orm import sessionmaker

from core.crypto import FernetKeyring, generate_key
from db.base import Base
from db import models  # noqa: F401
from db.session import build_engine

ENV_TEST_DATABASE_URL = "TEST_DATABASE_URL"


def make_keyring(versions=(1,), current: Optional[int] = None) -> FernetKeyring:
    """Claves generadas en cada ejecución: nunca hay claves fijas en el repositorio."""
    return FernetKeyring.from_keys({v: generate_key() for v in versions}, current)


def sqlite_session_factory() -> sessionmaker:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


class SqliteTestCase(unittest.TestCase):
    """Cada test recibe ``self.session`` sobre una base SQLite nueva."""

    def setUp(self) -> None:
        self.factory = sqlite_session_factory()
        self.session = self.factory()
        self.keyring = make_keyring()

    def tearDown(self) -> None:
        self.session.close()
        self.factory.kw["bind"].dispose()


@lru_cache(maxsize=1)
def postgres_server_url() -> Optional[str]:
    """URL de un servidor PostgreSQL real (sin base concreta) o ``None``."""
    explicit = os.environ.get(ENV_TEST_DATABASE_URL)
    if explicit:
        return explicit
    try:
        import pgserver
    except ImportError:
        return None
    try:
        data_dir = os.path.join(tempfile.gettempdir(), "huawei_inventory_pg_tests")
        server = pgserver.get_server(data_dir, cleanup_mode=None)
        return server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    except Exception:
        return None


def create_temp_database() -> Optional[str]:
    """Crea una base vacía y devuelve su URL (o ``None`` si no hay PostgreSQL)."""
    server = postgres_server_url()
    if server is None:
        return None
    if os.environ.get(ENV_TEST_DATABASE_URL):
        return server  # base proporcionada por el usuario: se usa tal cual
    import psycopg

    name = f"hi_test_{uuid.uuid4().hex[:10]}"
    admin = server.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    return server.rsplit("/", 1)[0] + f"/{name}"


def drop_temp_database(url: str) -> None:
    if os.environ.get(ENV_TEST_DATABASE_URL):
        return
    import psycopg

    base, name = url.rsplit("/", 1)
    with psycopg.connect(base.replace("postgresql+psycopg://", "postgresql://", 1) + "/postgres",
                         autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
