"""region catalog details: estado/zonas de flavors y tipos de disco por región (aditiva; escrita a mano)

Solo AÑADE columnas nullable a ``flavor_catalog`` y la tabla ``volume_type_catalog``.
No borra ni modifica filas existentes: las columnas nuevas quedan en NULL hasta la
siguiente consulta a Huawei (ECS ``ListFlavors`` / EVS ``CinderListVolumeTypes``).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-05 12:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0010'
down_revision: Union[str, None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    with op.batch_alter_table('flavor_catalog') as batch:
        batch.add_column(sa.Column('status', sa.String(length=16), nullable=True))
        batch.add_column(sa.Column('az_status', JSON, nullable=True))
        batch.add_column(sa.Column('architecture', sa.String(length=16), nullable=True))
        batch.add_column(sa.Column('cpu_name', sa.String(length=128), nullable=True))
        batch.add_column(sa.Column('gpu_name', sa.String(length=128), nullable=True))
        batch.add_column(sa.Column('max_bandwidth_gbps', sa.Numeric(precision=10, scale=2), nullable=True))
        batch.add_column(sa.Column('max_pps', sa.Integer(), nullable=True))
    op.create_table('volume_type_catalog',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('region', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('availability_zones', JSON, nullable=False),
    sa.Column('sold_out_zones', JSON, nullable=False),
    sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_volume_type_catalog')),
    sa.UniqueConstraint('region', 'name', name=op.f('uq_volume_type_catalog_region_name'))
    )
    op.create_index('ix_volume_type_catalog_region', 'volume_type_catalog', ['region'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_volume_type_catalog_region', table_name='volume_type_catalog')
    op.drop_table('volume_type_catalog')
    with op.batch_alter_table('flavor_catalog') as batch:
        for column in ('max_pps', 'max_bandwidth_gbps', 'gpu_name', 'cpu_name', 'architecture', 'az_status',
                       'status'):
            batch.drop_column(column)
