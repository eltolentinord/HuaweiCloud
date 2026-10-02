"""price and flavor catalog (comparador de costos por región; escrita a mano)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-02 11:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0008'
down_revision: Union[str, None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('price_catalog_entries',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('source_detail', sa.String(length=200), nullable=False),
    sa.Column('region', sa.String(length=64), nullable=False),
    sa.Column('product', sa.String(length=16), nullable=False),
    sa.Column('spec', sa.String(length=100), nullable=False),
    sa.Column('billing_mode', sa.String(length=16), nullable=False),
    sa.Column('size', sa.Integer(), server_default='0', nullable=False),
    sa.Column('amount', sa.Numeric(precision=20, scale=8), nullable=False),
    sa.Column('period', sa.String(length=8), nullable=False),
    sa.Column('currency', sa.String(length=8), nullable=False),
    sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("product IN ('ecs', 'evs', 'ip', 'bandwidth')", name=op.f('ck_price_catalog_entries_product')),
    sa.CheckConstraint("billing_mode IN ('on_demand', 'monthly')", name=op.f('ck_price_catalog_entries_billing_mode')),
    sa.CheckConstraint("period IN ('hour', 'month')", name=op.f('ck_price_catalog_entries_period')),
    sa.CheckConstraint("source IN ('huawei_bss')", name=op.f('ck_price_catalog_entries_source')),
    sa.CheckConstraint('amount >= 0', name=op.f('ck_price_catalog_entries_amount_non_negative')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_price_catalog_entries')),
    sa.UniqueConstraint('source', 'region', 'product', 'spec', 'billing_mode', 'size',
                        name='uq_price_catalog_key')
    )
    op.create_index('ix_price_catalog_lookup', 'price_catalog_entries',
                    ['region', 'product', 'spec', 'billing_mode'], unique=False)
    op.create_table('flavor_catalog',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('region', sa.String(length=64), nullable=False),
    sa.Column('flavor_id', sa.String(length=64), nullable=False),
    sa.Column('vcpus', sa.Integer(), nullable=False),
    sa.Column('ram_mb', sa.Integer(), nullable=False),
    sa.Column('performance_type', sa.String(length=64), nullable=True),
    sa.Column('generation', sa.String(length=32), nullable=True),
    sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_flavor_catalog')),
    sa.UniqueConstraint('region', 'flavor_id', name=op.f('uq_flavor_catalog_region_flavor_id'))
    )
    op.create_index('ix_flavor_catalog_region', 'flavor_catalog', ['region'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_flavor_catalog_region', table_name='flavor_catalog')
    op.drop_table('flavor_catalog')
    op.drop_index('ix_price_catalog_lookup', table_name='price_catalog_entries')
    op.drop_table('price_catalog_entries')
