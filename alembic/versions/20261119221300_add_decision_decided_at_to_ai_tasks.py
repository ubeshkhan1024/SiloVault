"""add decision and decided_at columns to ai_tasks for approval workflow

Revision ID: 20261119221300
Revises: 20261117221800
Create Date: 2026-11-19 22:13:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = '20261119221300'
down_revision = '20261117221800'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add decision and decided_at columns to ai_tasks table."""
    with op.batch_alter_table('ai_tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('decision', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Remove decision and decided_at columns from ai_tasks table."""
    with op.batch_alter_table('ai_tasks', schema=None) as batch_op:
        batch_op.drop_column('decided_at')
        batch_op.drop_column('decision')