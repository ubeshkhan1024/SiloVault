"""add restore snapshot columns and operation_generation to pending_deletions

Revision ID: 20261115130000
Revises: 20261114165042
Create Date: 2026-11-15 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = '20261115130000'
down_revision: Union[str, Sequence[str], None] = '20261114165042'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema using SQLite batch mode."""
    with op.batch_alter_table('pending_deletions', schema=None) as batch_op:
        batch_op.alter_column(
            'status',
            existing_type=sa.String(length=16),
            type_=sa.String(length=32),
            existing_nullable=False,
        )
        batch_op.add_column(sa.Column('original_file_id', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('original_logical_path', sa.String(length=1024), nullable=True))
        batch_op.add_column(sa.Column('original_mime_type', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('original_size_bytes', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('original_sha256', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('original_category', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('original_tags', sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column(
                'operation_generation',
                sa.Integer(),
                nullable=False,
                server_default=text('0'),
            )
        )


def downgrade() -> None:
    """Downgrade schema using SQLite batch mode."""
    with op.batch_alter_table('pending_deletions', schema=None) as batch_op:
        batch_op.drop_column('operation_generation')
        batch_op.drop_column('original_tags')
        batch_op.drop_column('original_category')
        batch_op.drop_column('original_sha256')
        batch_op.drop_column('original_size_bytes')
        batch_op.drop_column('original_mime_type')
        batch_op.drop_column('original_logical_path')
        batch_op.drop_column('original_file_id')
        batch_op.alter_column(
            'status',
            existing_type=sa.String(length=32),
            type_=sa.String(length=16),
            existing_nullable=False,
        )
