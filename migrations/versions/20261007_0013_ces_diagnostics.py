"""ces_diagnostics: eventos de alarma CES, incidentes y diagnóstico automático

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-07 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0013'
down_revision: Union[str, None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('ces_alarm_events',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('account_id', sa.Uuid(), nullable=True),
    sa.Column('alarm_id', sa.String(length=255), nullable=False),
    sa.Column('alarm_name', sa.String(length=500), nullable=True),
    sa.Column('namespace', sa.String(length=128), nullable=True),
    sa.Column('metric_name', sa.String(length=128), nullable=True),
    sa.Column('threshold', sa.Numeric(20, 8), nullable=True),
    sa.Column('observed_value', sa.Numeric(20, 8), nullable=True),
    sa.Column('alarm_level', sa.Integer(), nullable=True),
    sa.Column('resource_id', sa.String(length=255), nullable=True),
    sa.Column('alarm_status', sa.String(length=64), nullable=True),
    sa.Column('fired_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('idempotency_key', sa.String(length=255), nullable=False),
    sa.Column('raw_event_safe', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('status', sa.String(length=32), server_default='received', nullable=False),
    sa.Column('diagnostic_id', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['cloud_accounts.id'],
                            name=op.f('fk_ces_alarm_events_account_id_cloud_accounts'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ces_alarm_events')),
    sa.UniqueConstraint('idempotency_key', name=op.f('uq_ces_alarm_events_idempotency_key')),
    )
    op.create_index('ix_ces_alarm_events_account_fired', 'ces_alarm_events', ['account_id', 'fired_at'], unique=False)
    op.create_index(op.f('ix_ces_alarm_events_account_id'), 'ces_alarm_events', ['account_id'], unique=False)
    op.create_index('ix_ces_alarm_events_status', 'ces_alarm_events', ['status'], unique=False)

    op.create_table('diagnostic_incidents',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('client_id', sa.Uuid(), nullable=True),
    sa.Column('account_id', sa.Uuid(), nullable=True),
    sa.Column('event_id', sa.Uuid(), nullable=True),
    sa.Column('server_id', sa.Uuid(), nullable=True),
    sa.Column('ecs_name', sa.String(length=512), server_default='', nullable=False),
    sa.Column('ecs_instance_id', sa.String(length=255), server_default='', nullable=False),
    sa.Column('ecs_ip', sa.String(length=128), nullable=True),
    sa.Column('region', sa.String(length=64), server_default='', nullable=False),
    sa.Column('project_id_hw', sa.String(length=64), nullable=True),
    sa.Column('enterprise_project_id', sa.String(length=64), nullable=True),
    sa.Column('alarm_type', sa.String(length=64), server_default='unknown', nullable=False),
    sa.Column('metric_name', sa.String(length=128), nullable=True),
    sa.Column('threshold', sa.Numeric(20, 8), nullable=True),
    sa.Column('observed_value', sa.Numeric(20, 8), nullable=True),
    sa.Column('severity', sa.Integer(), nullable=True),
    sa.Column('alarm_fired_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=32), server_default='alert_received', nullable=False),
    sa.Column('possible_cause', sa.Text(), nullable=True),
    sa.Column('confidence', sa.String(length=16), nullable=True),
    sa.Column('pdf_data', sa.LargeBinary(), nullable=True),
    sa.Column('pdf_name', sa.String(length=255), nullable=True),
    sa.Column('report_json', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('reviewed', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('error_kind', sa.String(length=32), nullable=True),
    sa.Column('error_message_safe', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint(
        "status IN ('alert_received', 'validating', 'pending_diagnosis', 'connecting', "
        "'analyzing', 'generating_report', 'report_available', 'failed', 'no_server', 'recovered')",
        name=op.f('ck_diagnostic_incidents_status')
    ),
    sa.ForeignKeyConstraint(['account_id'], ['cloud_accounts.id'],
                            name=op.f('fk_diagnostic_incidents_account_id_cloud_accounts'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'],
                            name=op.f('fk_diagnostic_incidents_client_id_clients'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['event_id'], ['ces_alarm_events.id'],
                            name=op.f('fk_diagnostic_incidents_event_id_ces_alarm_events'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['server_id'], ['servers.id'],
                            name=op.f('fk_diagnostic_incidents_server_id_servers'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_diagnostic_incidents')),
    )
    op.create_index('ix_diagnostic_incidents_client_created', 'diagnostic_incidents', ['client_id', 'created_at'], unique=False)
    op.create_index('ix_diagnostic_incidents_account_created', 'diagnostic_incidents', ['account_id', 'created_at'], unique=False)
    op.create_index('ix_diagnostic_incidents_status', 'diagnostic_incidents', ['status'], unique=False)
    op.create_index('ix_diagnostic_incidents_server', 'diagnostic_incidents', ['server_id'], unique=False)
    op.create_index(op.f('ix_diagnostic_incidents_client_id'), 'diagnostic_incidents', ['client_id'], unique=False)

    op.create_table('diagnostic_commands',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('incident_id', sa.Uuid(), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('command', sa.String(length=255), nullable=False),
    sa.Column('args', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('exit_code', sa.Integer(), nullable=True),
    sa.Column('stdout_safe', sa.Text(), nullable=True),
    sa.Column('stderr_safe', sa.Text(), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['incident_id'], ['diagnostic_incidents.id'],
                            name=op.f('fk_diagnostic_commands_incident_id_diagnostic_incidents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_diagnostic_commands')),
    )
    op.create_index('ix_diagnostic_commands_incident_seq', 'diagnostic_commands', ['incident_id', 'sequence'], unique=False)

    op.create_table('diagnostic_evidence',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('incident_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('summary_safe', sa.Text(), nullable=True),
    sa.Column('data_json', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('relevance_score', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['incident_id'], ['diagnostic_incidents.id'],
                            name=op.f('fk_diagnostic_evidence_incident_id_diagnostic_incidents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_diagnostic_evidence')),
    )
    op.create_index('ix_diagnostic_evidence_incident', 'diagnostic_evidence', ['incident_id'], unique=False)

    op.create_table('diagnostic_audit_logs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('diagnostic_id_original', sa.Uuid(), nullable=False),
    sa.Column('incident_number', sa.String(length=64), nullable=True),
    sa.Column('account_id', sa.Uuid(), nullable=True),
    sa.Column('server_id', sa.Uuid(), nullable=True),
    sa.Column('server_name', sa.String(length=255), nullable=True),
    sa.Column('ecs_name', sa.String(length=512), nullable=True),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('performed_by', sa.String(length=200), nullable=False),
    sa.Column('performed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('metadata_safe', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_diagnostic_audit_logs')),
    )
    op.create_index('ix_diagnostic_audit_logs_account', 'diagnostic_audit_logs', ['account_id'], unique=False)
    op.create_index('ix_diagnostic_audit_logs_performed_at', 'diagnostic_audit_logs', ['performed_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_diagnostic_audit_logs_performed_at', table_name='diagnostic_audit_logs')
    op.drop_index('ix_diagnostic_audit_logs_account', table_name='diagnostic_audit_logs')
    op.drop_table('diagnostic_audit_logs')
    op.drop_index('ix_diagnostic_evidence_incident', table_name='diagnostic_evidence')
    op.drop_table('diagnostic_evidence')
    op.drop_index('ix_diagnostic_commands_incident_seq', table_name='diagnostic_commands')
    op.drop_table('diagnostic_commands')
    op.drop_index(op.f('ix_diagnostic_incidents_client_id'), table_name='diagnostic_incidents')
    op.drop_index('ix_diagnostic_incidents_server', table_name='diagnostic_incidents')
    op.drop_index('ix_diagnostic_incidents_status', table_name='diagnostic_incidents')
    op.drop_index('ix_diagnostic_incidents_account_created', table_name='diagnostic_incidents')
    op.drop_index('ix_diagnostic_incidents_client_created', table_name='diagnostic_incidents')
    op.drop_table('diagnostic_incidents')
    op.drop_index('ix_ces_alarm_events_status', table_name='ces_alarm_events')
    op.drop_index(op.f('ix_ces_alarm_events_account_id'), table_name='ces_alarm_events')
    op.drop_index('ix_ces_alarm_events_account_fired', table_name='ces_alarm_events')
    op.drop_table('ces_alarm_events')
