# coding: utf-8
"""Entorno de Alembic: URL desde DATABASE_URL y metadata de db.models."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from db import models  # noqa: F401  (registra las tablas en Base.metadata)
from db.base import Base
from db.session import database_url, safe_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    # Permite que los tests inyecten la URL sin tocar el entorno global.
    return config.attributes.get("database_url") or database_url()


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"}, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return

    url = _url()
    engine = create_engine(url, poolclass=pool.NullPool)
    print(f"Alembic -> {safe_url(url)}")
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
