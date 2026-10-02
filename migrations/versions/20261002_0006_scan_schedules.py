"""scan schedules (autogenerada; CHECK de trigger a mano)

- scan_schedules: escaneos periódicos ejecutados por el worker (manage.py worker).
- scan_runs.trigger admite 'schedule'.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-02 01:47:19.968878
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0006'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('scan_schedules',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('account_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('interval_minutes', sa.Integer(), nullable=False),
    sa.Column('services', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('regions', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_run_id', sa.Uuid(), nullable=True),
    sa.Column('last_triggered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_status', sa.String(length=32), nullable=True),
    sa.Column('last_error_safe', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('interval_minutes BETWEEN 15 AND 10080', name=op.f('ck_scan_schedules_interval_range')),
    sa.ForeignKeyConstraint(['account_id'], ['cloud_accounts.id'], name=op.f('fk_scan_schedules_account_id_cloud_accounts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['last_run_id'], ['scan_runs.id'], name=op.f('fk_scan_schedules_last_run_id_scan_runs'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scan_schedules'))
    )
    op.create_index(op.f('ix_scan_schedules_account_id'), 'scan_schedules', ['account_id'], unique=False)
    op.create_index('ix_scan_schedules_due', 'scan_schedules', ['enabled', 'next_run_at'], unique=False)
    op.drop_constraint(op.f('ck_scan_runs_trigger'), 'scan_runs', type_='check')
    op.create_check_constraint(op.f('ck_scan_runs_trigger'), 'scan_runs',
                               "trigger IN ('manual', 'api', 'cli', 'schedule')")


def downgrade() -> None:
    op.execute("UPDATE scan_runs SET trigger = 'manual' WHERE trigger = 'schedule'")
    op.drop_constraint(op.f('ck_scan_runs_trigger'), 'scan_runs', type_='check')
    op.create_check_constraint(op.f('ck_scan_runs_trigger'), 'scan_runs', "trigger IN ('manual', 'api', 'cli')")
    op.drop_index('ix_scan_schedules_due', table_name='scan_schedules')
    op.drop_index(op.f('ix_scan_schedules_account_id'), table_name='scan_schedules')
    op.drop_table('scan_schedules')
