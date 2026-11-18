"""add operation_type column to pending_deletions

Revision ID: 20261116185600
Revises: 20261115130000
Create Date: 2026-11-16 18:56:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = '20261116185600'
# Use previous migration as down_revision
# The previous migration added snapshot columns and operation_generation
# We should not alter any other columns.
down_revision: Union[str, Sequence[str], None] = '20261115130000'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Helper to add a check constraint ensuring only allowed values.
# SQLite supports check constraints.


def upgrade() -> None:
    """Add operation_type column with a check constraint.

    The column is nullable; missing values are left as NULL and are treated by the
    recovery service as unknown / unprocessed.  The check constraint applies
    only when the column is not NULL.
    """
    with op.batch_alter_table('pending_deletions', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'operation_type',
                sa.String(length=16),
                nullable=True,
            )
        )
        # SQLite check constraints must be added via op.create_check_constraint
        batch_op.create_check_constraint(
            'ck_pending_deletions_operation_type',
            "operation_type IS NULL OR operation_type IN ('CLEANUP','RESTORE')"
        )



def downgrade() -> None:
    """Remove operation_type column and its check constraint."""
    with op.batch_alter_table('pending_deletions', schema=None) as batch_op:
        batch_op.drop_constraint('ck_pending_deletions_operation_type', type_='check')
        batch_op.drop_column('operation_type')

""