"""audit events (autogenerada y revisada manualmente)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-02 09:48:57.443357
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0007'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('audit_events',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('actor_subject', sa.String(length=200), nullable=False),
    sa.Column('actor_kind', sa.String(length=20), nullable=False),
    sa.Column('client_id', sa.Uuid(), nullable=True),
    sa.Column('account_id', sa.Uuid(), nullable=True),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('target_type', sa.String(length=32), nullable=True),
    sa.Column('target_id', sa.String(length=64), nullable=True),
    sa.Column('request_id', sa.String(length=64), nullable=True),
    sa.Column('details', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_events'))
    )
    op.create_index('ix_audit_events_action', 'audit_events', ['action'], unique=False)
    op.create_index('ix_audit_events_client_occurred', 'audit_events', ['client_id', 'occurred_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_audit_events_client_occurred', table_name='audit_events')
    op.drop_index('ix_audit_events_action', table_name='audit_events')
    op.drop_table('audit_events')
