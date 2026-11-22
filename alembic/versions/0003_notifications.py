"""notifications

Phone notifications: push_subscriptions (devices), app_settings (signing keys),
job_runs (once-per-day job ledger) and per-user notification preferences.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('app_settings',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_app_settings'))
    )
    op.create_table('job_runs',
    sa.Column('job', sa.String(length=64), nullable=False),
    sa.Column('run_on', sa.Date(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sent', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('job', 'run_on', name=op.f('pk_job_runs'))
    )
    op.create_table('push_subscriptions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('endpoint', sa.Text(), nullable=False),
    sa.Column('p256dh', sa.String(length=255), nullable=False),
    sa.Column('auth', sa.String(length=255), nullable=False),
    sa.Column('device_label', sa.String(length=120), nullable=True),
    sa.Column('failure_count', sa.Integer(), nullable=False),
    sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_push_subscriptions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_push_subscriptions')),
    sa.UniqueConstraint('endpoint', name=op.f('uq_push_subscriptions_endpoint'))
    )
    op.create_index(op.f('ix_push_subscriptions_user_id'), 'push_subscriptions', ['user_id'], unique=False)
    op.add_column('users', sa.Column('notify_morning', sa.Boolean(), server_default=sa.text('true'), nullable=False))
    op.add_column('users', sa.Column('notify_evening', sa.Boolean(), server_default=sa.text('true'), nullable=False))


def downgrade() -> None:
    op.drop_column('users', 'notify_evening')
    op.drop_column('users', 'notify_morning')
    op.drop_index(op.f('ix_push_subscriptions_user_id'), table_name='push_subscriptions')
    op.drop_table('push_subscriptions')
    op.drop_table('job_runs')
    op.drop_table('app_settings')
