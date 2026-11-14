"""
Create AI tasks table

Revision ID: 20261114165042
Revises: 597b2cc67a4a
Create Date: 2026-11-14 16:50:42.000000
"""
from alembic import op
import sqlalchemy as sa
import backend.models.unified_file  # noqa: F401  # registers UTCDateTime

# revision identifiers, used by Alembic.
revision = '20261114165042'
down_revision = '597b2cc67a4a'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        'ai_tasks',
        sa.Column('id', sa.String(length=45), primary_key=True),
        sa.Column('file_id', sa.String(length=41), nullable=True),
        sa.Column('task_type', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='PENDING'),
        sa.Column('retries', sa.Integer, nullable=False, default=0),
        sa.Column('max_retries', sa.Integer, nullable=False, default=3),
        sa.Column('created_at', backend.models.unified_file.UTCDateTime(timezone=True), nullable=False),
        sa.Column('started_at', backend.models.unified_file.UTCDateTime(timezone=True)),
        sa.Column('completed_at', backend.models.unified_file.UTCDateTime(timezone=True)),
        sa.Column('cancellation_requested_at', backend.models.unified_file.UTCDateTime(timezone=True)),
        sa.Column('claim_token', sa.String(length=36)),
        sa.Column('worker_id', sa.String(length=64)),
        sa.Column('lease_expires_at', backend.models.unified_file.UTCDateTime(timezone=True)),
        sa.Column('proposal_json', sa.JSON),
        sa.Column('error_message', sa.String(length=1024)),
        sa.ForeignKeyConstraint(['file_id'], ['unified_file_records.id'], ondelete='SET NULL'),
    )
    with op.batch_alter_table('ai_tasks', schema=None) as batch_op:
        batch_op.create_index('ix_ai_tasks_status', ['status'], unique=False)
        batch_op.create_index('ix_ai_tasks_lease_expires', ['lease_expires_at'], unique=False)
        batch_op.create_index('ix_ai_tasks_file_id', ['file_id'], unique=False)

def downgrade() -> None:
    op.drop_table('ai_tasks')
