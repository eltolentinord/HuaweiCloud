"""one active scan per account (autogenerada y revisada manualmente)

Índice único parcial: como mucho un ScanRun pending/running por cuenta (Fase 3B).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-01 22:56:04.136746
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Si una base de la Fase 3A tuviera varios escaneos activos de la misma cuenta,
    # el índice no podría crearse: se conserva el más reciente y el resto se cierra.
    op.execute("""
        UPDATE scan_runs SET status = 'failed',
               error_message_safe = 'Cerrado al migrar: había otro escaneo activo de la misma cuenta.'
        WHERE status IN ('pending', 'running')
          AND id NOT IN (SELECT DISTINCT ON (account_id) id FROM scan_runs
                         WHERE status IN ('pending', 'running')
                         ORDER BY account_id, created_at DESC)
    """)
    op.create_index('uq_scan_runs_one_active_per_account', 'scan_runs', ['account_id'], unique=True, postgresql_where=sa.text("status IN ('pending', 'running')"), sqlite_where=sa.text("status IN ('pending', 'running')"))


def downgrade() -> None:
    op.drop_index('uq_scan_runs_one_active_per_account', table_name='scan_runs', postgresql_where=sa.text("status IN ('pending', 'running')"), sqlite_where=sa.text("status IN ('pending', 'running')"))
