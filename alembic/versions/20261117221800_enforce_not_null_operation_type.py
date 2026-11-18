"""Enforce NOT NULL operation_type on pending_deletions

Revision ID: 20261117221800
Revises: 20261116185600
Create Date: 2026-11-17 22:18:00.000000

Stage B: make operation_type mandatory.

- Backfills historical NULL operation_type rows as 'CLEANUP'.
  Rationale: the only production creation path is DELETE /files/{id},
  which exclusively performs cleanup. No restore creation path exists
  in the codebase at the time of this migration.
- Validates that every row has a known status before backfilling.
- Enforces NOT NULL and tightens the check constraint to
  ('CLEANUP', 'RESTORE') only.

All operations run inside the migration-managed transaction. Alembic
owns the commit/rollback boundary; there is no manual COMMIT.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = '20261117221800'
down_revision: Union[str, Sequence[str], None] = '20261116185600'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Statuses the recovery service understands. Any row outside this set is
# treated as unclassifiable and aborts the migration rather than guessing.
_KNOWN_STATUSES = ("PENDING", "PROCESSING", "FAILED")


def _validate_and_backfill(conn) -> None:
    """Validate historical rows, then backfill NULL operation_type as CLEANUP.

    Runs inside the Alembic-managed transaction. Raises on unclassifiable
    data so the migration rolls back atomically.
    """
    # 1. Detect any row with an unknown status. These cannot be classified
    #    safely, so we abort before touching any data.
    placeholders = ", ".join(f":s{i}" for i in range(len(_KNOWN_STATUSES)))
    params = {f"s{i}": status for i, status in enumerate(_KNOWN_STATUSES)}
    unknown_status_rows = conn.execute(
        text(
            f"SELECT id, status FROM pending_deletions "
            f"WHERE status NOT IN ({placeholders})"
        ),
        params,
    ).fetchall()

    if unknown_status_rows:
        raise ValueError(
            "Cannot enforce NOT NULL operation_type: found rows with "
            f"unknown status: {[(r[0], r[1]) for r in unknown_status_rows]}. "
            "Reconcile these rows manually before migrating."
        )

    # 2. Count and backfill NULL operation_type rows as CLEANUP.
    null_rows = conn.execute(
        text(
            "SELECT id, provider, provider_file_id, status, created_at "
            "FROM pending_deletions WHERE operation_type IS NULL"
        )
    ).fetchall()

    if null_rows:
        conn.execute(
            text(
                "UPDATE pending_deletions "
                "SET operation_type = 'CLEANUP' "
                "WHERE operation_type IS NULL"
            )
        )
        # Log each backfilled row so the operator has a record of what changed.
        import logging
        logger = logging.getLogger(__name__)
        for row in null_rows:
            logger.info(
                "operation_type backfill: id=%s provider=%s file=%s status=%s created=%s -> CLEANUP",
                row[0], row[1], row[2], row[3], row[4],
            )


def upgrade() -> None:
    """Backfill NULL operation_type, validate, then enforce NOT NULL.

    SQLite requires table recreation for NOT NULL changes, so we use
    batch_alter_table. The backfill/validation runs on the raw connection
    within the same transaction that the batch operation uses.
    """
    conn = op.get_bind()
    _validate_and_backfill(conn)

    with op.batch_alter_table('pending_deletions', schema=None) as batch_op:
        # Drop the old constraint that permits NULL.
        batch_op.drop_constraint(
            'ck_pending_deletions_operation_type', type_='check'
        )

        # Enforce NOT NULL.
        batch_op.alter_column(
            'operation_type',
            existing_type=sa.String(length=16),
            nullable=False,
        )

        # Add the tightened constraint (no IS NULL branch).
        batch_op.create_check_constraint(
            'ck_pending_deletions_operation_type',
            "operation_type IN ('CLEANUP', 'RESTORE')"
        )


def downgrade() -> None:
    """Revert to nullable operation_type with the original check constraint.

    Existing values are preserved; only the nullability and constraint
    are relaxed. No data is nulled out.
    """
    with op.batch_alter_table('pending_deletions', schema=None) as batch_op:
        batch_op.drop_constraint(
            'ck_pending_deletions_operation_type', type_='check'
        )
        batch_op.alter_column(
            'operation_type',
            existing_type=sa.String(length=16),
            nullable=True,
        )
        batch_op.create_check_constraint(
            'ck_pending_deletions_operation_type',
            "operation_type IS NULL OR operation_type IN ('CLEANUP', 'RESTORE')"
        )
