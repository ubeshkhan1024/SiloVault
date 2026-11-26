"""H1.5.3 credentials_ref normalized uniqueness

Revision ID: b7c4e1a92f30
Revises: 771ae2a40a8a
Create Date: 2026-11-25 12:00:00.000000

Adds a database-level backstop for credential-reference uniqueness.

The index is a partial unique expression index over
``trim(credentials_ref, char(9,10,11,12,13,32))``.  The expression strips the
common ASCII whitespace characters (space, TAB, LF, VT, FF, CR) from both ends,
so a reference stored with padding cannot coexist with its trimmed form.
``WHERE credentials_ref IS NOT NULL`` keeps credential-less accounts (e.g.
LOCAL) out of the index; SQLite would allow multiple NULLs anyway, but the
partial predicate states the intent explicitly.

Notes
-----
* No data is rewritten, normalized, or deleted.  If the database already holds
  two references that normalize to the same value, index creation fails and the
  migration aborts; resolve those duplicates explicitly first.
* Python's ``str.strip()`` in ``AccountManager`` remains authoritative: it also
  covers Unicode whitespace that SQLite's ``trim()`` does not.
* The index is intentionally **not** declared on the ``StorageAccount`` model.
  SQLite reflection skips expression-based indexes, so declaring it in metadata
  would make Alembic autogenerate try to recreate it on every run.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c4e1a92f30'
down_revision: Union[str, Sequence[str], None] = '771ae2a40a8a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Index name shared with the migration tests.
_INDEX_NAME = "uq_storage_accounts_credentials_ref_trimmed"

#: ASCII whitespace stripped from both ends of a reference: space, TAB, LF,
#: VT, FF, CR.
_TRIM_CHARACTERS = "char(9,10,11,12,13,32)"


def upgrade() -> None:
    """Create the partial unique expression index (no data changes)."""
    op.create_index(
        _INDEX_NAME,
        "storage_accounts",
        [sa.text(f"trim(credentials_ref, {_TRIM_CHARACTERS})")],
        unique=True,
        sqlite_where=sa.text("credentials_ref IS NOT NULL"),
    )


def downgrade() -> None:
    """Remove only this index; the table, its rows, and other indexes remain."""
    op.drop_index(_INDEX_NAME, table_name="storage_accounts")
