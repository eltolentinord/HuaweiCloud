"""scan warnings: denied/unavailable statuses (columnas autogeneradas; CHECK a mano)

Tras la primera prueba real: un 403 o un 401 de autorización IAM (p. ej. VPN.0003)
es un AVISO, no un fallo del escaneo.

- scan_tasks.status: + ``denied``, ``unavailable``
- scan_runs.status:  + ``completed_with_warnings``
- scan_runs.total_warnings, scan_tasks.iam_action

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-01 22:33:34.508298
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUN_STATUSES_0002 = ("pending", "running", "completed", "completed_with_errors", "failed")
RUN_STATUSES_0003 = ("pending", "running", "completed", "completed_with_warnings",
                     "completed_with_errors", "failed")
TASK_STATUSES_0002 = ("pending", "running", "succeeded", "partial", "failed", "skipped")
TASK_STATUSES_0003 = ("pending", "running", "succeeded", "partial", "denied", "unavailable",
                      "failed", "skipped")


def _in(values) -> str:
    return "status IN ({})".format(", ".join(repr(v) for v in values))


def _replace_check(table: str, values) -> None:
    name = op.f(f"ck_{table}_status")
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, _in(values))


def upgrade() -> None:
    op.add_column('scan_runs', sa.Column('total_warnings', sa.Integer(), server_default='0', nullable=False))
    op.add_column('scan_tasks', sa.Column('iam_action', sa.String(length=128), nullable=True))
    _replace_check("scan_runs", RUN_STATUSES_0003)
    _replace_check("scan_tasks", TASK_STATUSES_0003)


def downgrade() -> None:
    # Los estados nuevos se reasignan al equivalente más conservador de 0002.
    op.execute("UPDATE scan_runs SET status = 'completed_with_errors' WHERE status = 'completed_with_warnings'")
    op.execute("UPDATE scan_tasks SET status = 'failed' WHERE status IN ('denied', 'unavailable')")
    _replace_check("scan_runs", RUN_STATUSES_0002)
    _replace_check("scan_tasks", TASK_STATUSES_0002)
    op.drop_column('scan_tasks', 'iam_action')
    op.drop_column('scan_runs', 'total_warnings')
