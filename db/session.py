# coding: utf-8
"""Configuración de la base de datos (solo variables de entorno) y sesiones reutilizables.

    DATABASE_URL=postgresql+psycopg://USUARIO:CLAVE@localhost:5432/huawei_inventory

La base de datos es opcional: sin ``DATABASE_URL`` la aplicación sigue funcionando
con el flujo actual (credenciales en el formulario).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator, Optional

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

ENV_DATABASE_URL = "DATABASE_URL"
ENV_DATABASE_ECHO = "DATABASE_ECHO"


class DatabaseNotConfiguredError(RuntimeError):
    """``DATABASE_URL`` no está definida."""


def database_url(environ: Optional[dict] = None) -> str:
    url = ((environ or os.environ).get(ENV_DATABASE_URL) or "").strip()
    if not url:
        raise DatabaseNotConfiguredError(
            f"Define la variable de entorno {ENV_DATABASE_URL} (ver .env.example)."
        )
    return url


def safe_url(url: str) -> str:
    """URL sin contraseña, apta para logs y mensajes."""
    return make_url(url).render_as_string(hide_password=True)


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_connection, _record):  # pragma: no cover - trivial
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def build_engine(url: str, *, echo: bool = False) -> Engine:
    """Engine con ``pool_pre_ping`` (PostgreSQL) o FKs activas (SQLite, solo tests)."""
    if url.startswith("sqlite"):
        # Una sola conexión compartida (bases en memoria de tests, usadas desde varios hilos).
        engine = create_engine(url, echo=echo, poolclass=StaticPool, hide_parameters=True,
                               connect_args={"check_same_thread": False})
        _enable_sqlite_foreign_keys(engine)
        return engine
    # hide_parameters: los errores SQL nunca incluyen valores (raw, nombres...) en logs/excepciones.
    return create_engine(url, echo=echo, pool_pre_ping=True, pool_size=5, max_overflow=10,
                         hide_parameters=True)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    echo = os.environ.get(ENV_DATABASE_ECHO, "").lower() in ("1", "true", "yes")
    return build_engine(database_url(), echo=echo)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def session_scope(factory: Optional[sessionmaker] = None) -> Iterator[Session]:
    """Sesión transaccional: commit al terminar, rollback ante cualquier error."""
    session = (factory or get_session_factory())()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db_session_factory() -> sessionmaker:
    """Dependencia FastAPI para tareas en segundo plano (abren sus propias sesiones)."""
    return get_session_factory()


def get_db() -> Iterator[Session]:
    """Dependencia FastAPI."""
    with session_scope() as session:
        yield session
