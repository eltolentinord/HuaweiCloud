"""calculator: suscripción anual, tráfico, RDS/OBS en el catálogo de precios (aditiva; escrita a mano)

- ``price_catalog_entries``: los CHECK aceptan valores NUEVOS (``yearly``, ``year``/``gb``,
  ``traffic``/``rds``/``rds_storage``/``obs_storage``); las filas existentes no cambian.
- ``rds_flavor_catalog``: flavors de RDS por región/motor (RDS ``ListFlavors``).
- ``bss_code_catalog``: códigos de servicio/recurso/uso confirmados con BSS
  (``ListServiceTypes`` / ``ListResourceTypes`` / ``ListUsageTypes``).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-05 15:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0011'
down_revision: Union[str, None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
TABLE = 'price_catalog_entries'
OLD = {
    'product': ('ecs', 'evs', 'ip', 'bandwidth'),
    'billing_mode': ('on_demand', 'monthly'),
    'period': ('hour', 'month'),
}
NEW = {
    'product': ('ecs', 'evs', 'ip', 'bandwidth', 'traffic', 'rds', 'rds_storage', 'obs_storage'),
    'billing_mode': ('on_demand', 'monthly', 'yearly'),
    'period': ('hour', 'month', 'year', 'gb'),
}


def _in(column: str, values: tuple) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _replace_checks(values: dict) -> None:
    for column, allowed in values.items():
        name = op.f(f'ck_{TABLE}_{column}')
        op.drop_constraint(name, TABLE, type_='check')
        op.create_check_constraint(name, TABLE, _in(column, allowed))


def upgrade() -> None:
    _replace_checks(NEW)
    op.create_table('rds_flavor_catalog',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('region', sa.String(length=64), nullable=False),
    sa.Column('engine', sa.String(length=32), nullable=False),
    sa.Column('engine_version', sa.String(length=32), nullable=False),
    sa.Column('spec_code', sa.String(length=100), nullable=False),
    sa.Column('vcpus', sa.Integer(), nullable=False),
    sa.Column('ram_gb', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('instance_mode', sa.String(length=16), nullable=False),
    sa.Column('az_status', JSON, nullable=True),
    sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_rds_flavor_catalog')),
    sa.UniqueConstraint('region', 'engine', 'engine_version', 'spec_code',
                        name=op.f('uq_rds_flavor_catalog_region_engine_engine_version_spec_code'))
    )
    op.create_index('ix_rds_flavor_catalog_region_engine', 'rds_flavor_catalog', ['region', 'engine'], unique=False)
    op.create_table('bss_code_catalog',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=True),
    sa.Column('parent_code', sa.String(length=128), nullable=True),
    sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("kind IN ('service', 'resource', 'usage')", name=op.f('ck_bss_code_catalog_kind')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_bss_code_catalog')),
    sa.UniqueConstraint('kind', 'code', name=op.f('uq_bss_code_catalog_kind_code'))
    )


def downgrade() -> None:
    op.drop_table('bss_code_catalog')
    op.drop_index('ix_rds_flavor_catalog_region_engine', table_name='rds_flavor_catalog')
    op.drop_table('rds_flavor_catalog')
    # Solo se puede volver a los CHECK antiguos si no hay filas con los valores nuevos.
    op.execute(f"DELETE FROM {TABLE} WHERE billing_mode = 'yearly' OR period IN ('year', 'gb') "
               "OR product IN ('traffic', 'rds', 'rds_storage', 'obs_storage')")
    _replace_checks(OLD)
