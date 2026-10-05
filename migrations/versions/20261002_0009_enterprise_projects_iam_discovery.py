"""enterprise projects + informe de descubrimiento IAM (solo aditiva; escrita a mano)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-02 13:00:00

Añade la tabla ``enterprise_projects`` y dos columnas NULLABLE en ``cloud_accounts``
(``last_discovery_at``, ``discovery_report``). No modifica ni borra datos existentes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0009'
down_revision: Union[str, None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('cloud_accounts', sa.Column('last_discovery_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('cloud_accounts', sa.Column(
        'discovery_report', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'),
        nullable=True))
    op.create_table('enterprise_projects',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('account_id', sa.Uuid(), nullable=False),
    sa.Column('huawei_ep_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('status', sa.Integer(), nullable=True),
    sa.Column('ep_type', sa.String(length=16), nullable=True),
    sa.Column('present', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('discovered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['cloud_accounts.id'], name=op.f('fk_enterprise_projects_account_id_cloud_accounts'),
                            ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_enterprise_projects')),
    sa.UniqueConstraint('account_id', 'huawei_ep_id', name=op.f('uq_enterprise_projects_account_id_huawei_ep_id'))
    )
    op.create_index(op.f('ix_enterprise_projects_account_id'), 'enterprise_projects', ['account_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_enterprise_projects_account_id'), table_name='enterprise_projects')
    op.drop_table('enterprise_projects')
    op.drop_column('cloud_accounts', 'discovery_report')
    op.drop_column('cloud_accounts', 'last_discovery_at')
