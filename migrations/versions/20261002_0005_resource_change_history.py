"""resource change history + scan sequence (autogenerada y revisada manualmente)

- resource_changes: eventos created/updated/restored/deleted por recurso y escaneo.
- scan_runs.sequence: número de escaneo por cuenta (orden total y estable; la marca
  de tiempo no basta: dos escaneos pueden caer en el mismo instante). Se rellena para
  los escaneos existentes por orden de creación.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-02 01:26:39.423639
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('scan_runs', sa.Column('sequence', sa.Integer(), nullable=True))
    op.execute("""
        UPDATE scan_runs SET sequence = numbered.n
        FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY created_at, id) AS n
              FROM scan_runs) AS numbered
        WHERE scan_runs.id = numbered.id
    """)
    op.alter_column('scan_runs', 'sequence', nullable=False)
    op.create_unique_constraint(op.f('uq_scan_runs_account_id_sequence'), 'scan_runs', ['account_id', 'sequence'])
    op.create_table('resource_changes',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('account_id', sa.Uuid(), nullable=False),
    sa.Column('resource_id', sa.Uuid(), nullable=False),
    sa.Column('scan_run_id', sa.Uuid(), nullable=False),
    sa.Column('change_type', sa.String(length=16), nullable=False),
    sa.Column('service', sa.String(length=32), nullable=False),
    sa.Column('resource_type', sa.String(length=64), nullable=False),
    sa.Column('provider_id', sa.String(length=255), nullable=False),
    sa.Column('region', sa.String(length=64), server_default='', nullable=False),
    sa.Column('name', sa.String(length=512), nullable=True),
    sa.Column('changed_fields', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('raw_hash_before', sa.String(length=64), nullable=True),
    sa.Column('raw_hash_after', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("change_type IN ('created', 'updated', 'restored', 'deleted')", name=op.f('ck_resource_changes_change_type')),
    sa.ForeignKeyConstraint(['account_id'], ['cloud_accounts.id'], name=op.f('fk_resource_changes_account_id_cloud_accounts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], name=op.f('fk_resource_changes_resource_id_resources'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['scan_run_id'], ['scan_runs.id'], name=op.f('fk_resource_changes_scan_run_id_scan_runs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_resource_changes'))
    )
    op.create_index('ix_resource_changes_account_created', 'resource_changes', ['account_id', 'created_at'], unique=False)
    op.create_index('ix_resource_changes_resource_created', 'resource_changes', ['resource_id', 'created_at'], unique=False)
    op.create_index('ix_resource_changes_run_type', 'resource_changes', ['scan_run_id', 'change_type'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_resource_changes_run_type', table_name='resource_changes')
    op.drop_index('ix_resource_changes_resource_created', table_name='resource_changes')
    op.drop_index('ix_resource_changes_account_created', table_name='resource_changes')
    op.drop_table('resource_changes')
    op.drop_constraint(op.f('uq_scan_runs_account_id_sequence'), 'scan_runs', type_='unique')
    op.drop_column('scan_runs', 'sequence')
