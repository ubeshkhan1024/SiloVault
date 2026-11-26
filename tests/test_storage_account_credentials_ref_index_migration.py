"""Isolated migration tests for the H1.5.3 credentials_ref uniqueness index.

Every test runs Alembic against its own temporary SQLite file under
``tmp_path``.  The real application database is never opened, inspected, or
migrated.

The index is a partial unique expression index:

    CREATE UNIQUE INDEX uq_storage_accounts_credentials_ref_trimmed
        ON storage_accounts (trim(credentials_ref, char(9,10,11,12,13,32)))
        WHERE credentials_ref IS NOT NULL

``sqlalchemy.inspect().get_indexes()`` deliberately cannot be used here: SQLite
reflection skips expression-based indexes, so the assertions read
``sqlite_master.sql`` and ``PRAGMA index_list`` instead.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

PREVIOUS_REVISION = "771ae2a40a8a"
HEAD_REVISION = "b7c4e1a92f30"

INDEX_NAME = "uq_storage_accounts_credentials_ref_trimmed"
PROVIDER_INDEX_NAME = "ix_storage_accounts_provider_id"

EXPECTED_INDEX_SQL = (
    "CREATE UNIQUE INDEX uq_storage_accounts_credentials_ref_trimmed "
    "ON storage_accounts (trim(credentials_ref, char(9,10,11,12,13,32))) "
    "WHERE credentials_ref IS NOT NULL"
)

#: Variants that normalize to ``ref-a`` once ASCII whitespace is trimmed.
ASCII_WHITESPACE_VARIANTS = [
    "ref-a\t",
    "\tref-a",
    "ref-a\n",
    "\nref-a",
    "ref-a\r",
    "\r\n\t ref-a \t\r\n",
    "\x0bref-a",
    "\x0cref-a",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_alembic_cmd(
    db_path: Path, cmd: str, extra_args: list[str] | None = None
) -> None:
    """Run an Alembic command against *db_path* via the DATABASE_PATH override.

    Mirrors the established in-process pattern from ``tests/test_alembic.py``:
    the environment variable is set for the duration of the command, the
    settings module is reloaded so ``alembic/env.py`` resolves the temporary
    file, and both are restored afterwards.
    """
    from importlib import reload

    import backend.config as config_module

    os.environ["DATABASE_PATH"] = str(db_path)
    try:
        reload(config_module)
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(ALEMBIC_INI))
        getattr(command, cmd)(cfg, *(extra_args or []))
    finally:
        os.environ.pop("DATABASE_PATH", None)
        reload(config_module)


@contextmanager
def _db(db_path: Path):
    """Yield a transactional connection to the temporary database."""
    engine = sa.create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    try:
        with engine.begin() as conn:
            yield conn
    finally:
        engine.dispose()


def _index_sql(conn: sa.Connection, name: str) -> str | None:
    return conn.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:name"),
        {"name": name},
    ).scalar()


def _index_row(conn: sa.Connection, name: str):
    """Return the ``PRAGMA index_list`` row for *name*: (seq, name, unique, origin, partial)."""
    rows = conn.execute(sa.text("PRAGMA index_list('storage_accounts')")).fetchall()
    for row in rows:
        if row[1] == name:
            return row
    return None


def _insert_account(conn: sa.Connection, account_id: str, credentials_ref) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO storage_accounts "
            "(id, provider_id, display_name, credentials_ref, status, "
            " configuration, created_at, updated_at) "
            "VALUES (:id, 'LOCAL', :display_name, :ref, 'pending', '{}', :ts, :ts)"
        ),
        {
            "id": account_id,
            "display_name": f"Account {account_id}",
            "ref": credentials_ref,
            "ts": "2026-01-01 00:00:00",
        },
    )


def _refs(conn: sa.Connection) -> list[str | None]:
    return [
        row[0]
        for row in conn.execute(
            sa.text("SELECT credentials_ref FROM storage_accounts ORDER BY id")
        ).fetchall()
    ]


def _upgrade(db_path: Path) -> None:
    _run_alembic_cmd(db_path, "upgrade", ["head"])


# ---------------------------------------------------------------------------
# Index creation
# ---------------------------------------------------------------------------


def test_upgrade_creates_exact_partial_unique_index(tmp_path):
    db_path = tmp_path / "index_creation.db"

    _upgrade(db_path)

    with _db(db_path) as conn:
        assert _index_sql(conn, INDEX_NAME) == EXPECTED_INDEX_SQL

        row = _index_row(conn, INDEX_NAME)
        assert row is not None
        assert row[2] == 1, "index must be UNIQUE"
        assert row[4] == 1, "index must be partial (WHERE credentials_ref IS NOT NULL)"

        assert _index_sql(conn, PROVIDER_INDEX_NAME) is not None
        revision = conn.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar()
        assert revision == HEAD_REVISION


# ---------------------------------------------------------------------------
# Allowed rows
# ---------------------------------------------------------------------------


def test_multiple_null_references_are_allowed(tmp_path):
    db_path = tmp_path / "nulls.db"
    _upgrade(db_path)

    with _db(db_path) as conn:
        _insert_account(conn, "acc-1", None)
        _insert_account(conn, "acc-2", None)
        _insert_account(conn, "acc-3", None)

    with _db(db_path) as conn:
        assert _refs(conn) == [None, None, None]


def test_distinct_references_are_allowed(tmp_path):
    db_path = tmp_path / "distinct.db"
    _upgrade(db_path)

    with _db(db_path) as conn:
        _insert_account(conn, "acc-1", "ref-a")
        _insert_account(conn, "acc-2", "ref-b")
        _insert_account(conn, "acc-3", "storage-account:11111111-1111-1111-1111-111111111111")

    with _db(db_path) as conn:
        assert _refs(conn) == [
            "ref-a",
            "ref-b",
            "storage-account:11111111-1111-1111-1111-111111111111",
        ]


# ---------------------------------------------------------------------------
# Rejected duplicates
# ---------------------------------------------------------------------------


def test_exact_duplicate_reference_is_rejected(tmp_path):
    db_path = tmp_path / "exact_duplicate.db"
    _upgrade(db_path)

    with _db(db_path) as conn:
        _insert_account(conn, "acc-1", "ref-a")

    with pytest.raises(IntegrityError):
        with _db(db_path) as conn:
            _insert_account(conn, "acc-2", "ref-a")

    with _db(db_path) as conn:
        assert _refs(conn) == ["ref-a"]


def test_space_padded_duplicate_reference_is_rejected(tmp_path):
    db_path = tmp_path / "space_padded.db"
    _upgrade(db_path)

    with _db(db_path) as conn:
        _insert_account(conn, "acc-1", "ref-a")

    for position, variant in enumerate(("  ref-a  ", " ref-a", "ref-a ")):
        with pytest.raises(IntegrityError):
            with _db(db_path) as conn:
                _insert_account(conn, f"dup-{position}", variant)

    with _db(db_path) as conn:
        assert _refs(conn) == ["ref-a"]


@pytest.mark.parametrize("variant", ASCII_WHITESPACE_VARIANTS)
def test_ascii_whitespace_variant_is_rejected(tmp_path, variant):
    db_path = tmp_path / "ascii_whitespace.db"
    _upgrade(db_path)

    with _db(db_path) as conn:
        _insert_account(conn, "acc-1", "ref-a")

    with pytest.raises(IntegrityError):
        with _db(db_path) as conn:
            _insert_account(conn, "acc-2", variant)

    with _db(db_path) as conn:
        assert _refs(conn) == ["ref-a"]


def test_unicode_whitespace_variant_is_not_covered(tmp_path):
    """Documented limitation: SQLite ``trim()`` only strips the listed ASCII set.

    Python's ``str.strip()`` in ``AccountManager`` rejects such a reference
    before it reaches the database; the index is a backstop, not the primary
    guard.
    """
    db_path = tmp_path / "unicode_whitespace.db"
    _upgrade(db_path)

    with _db(db_path) as conn:
        _insert_account(conn, "acc-1", "ref-a")
        _insert_account(conn, "acc-2", "\u00a0ref-a")  # NO-BREAK SPACE

    with _db(db_path) as conn:
        assert _refs(conn) == ["ref-a", "\u00a0ref-a"]


# ---------------------------------------------------------------------------
# Existing data
# ---------------------------------------------------------------------------


def test_existing_rows_survive_upgrade(tmp_path):
    db_path = tmp_path / "existing_rows.db"
    _run_alembic_cmd(db_path, "upgrade", [PREVIOUS_REVISION])

    with _db(db_path) as conn:
        _insert_account(conn, "acc-null", None)
        _insert_account(conn, "acc-a", "ref-a")
        _insert_account(conn, "acc-b", "ref-b")

    _upgrade(db_path)

    with _db(db_path) as conn:
        # Ordered by id: acc-a, acc-b, acc-null.
        assert _refs(conn) == ["ref-a", "ref-b", None]
        assert _index_sql(conn, INDEX_NAME) == EXPECTED_INDEX_SQL
        assert _index_sql(conn, PROVIDER_INDEX_NAME) is not None


# ---------------------------------------------------------------------------
# Downgrade and re-upgrade
# ---------------------------------------------------------------------------


def test_downgrade_removes_only_the_new_index(tmp_path):
    db_path = tmp_path / "downgrade.db"
    _upgrade(db_path)

    with _db(db_path) as conn:
        _insert_account(conn, "acc-null", None)
        _insert_account(conn, "acc-a", "ref-a")
        _insert_account(conn, "acc-b", "ref-b")

    _run_alembic_cmd(db_path, "downgrade", ["-1"])

    with _db(db_path) as conn:
        assert _index_sql(conn, INDEX_NAME) is None
        assert _index_row(conn, INDEX_NAME) is None
        # The unrelated provider index and every row must survive.
        assert _index_sql(conn, PROVIDER_INDEX_NAME) is not None
        assert _refs(conn) == ["ref-a", "ref-b", None]
        revision = conn.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar()
        assert revision == PREVIOUS_REVISION


def test_downgrade_allows_duplicates_again(tmp_path):
    db_path = tmp_path / "downgrade_duplicates.db"
    _upgrade(db_path)
    _run_alembic_cmd(db_path, "downgrade", ["-1"])

    with _db(db_path) as conn:
        _insert_account(conn, "acc-1", "ref-a")
        _insert_account(conn, "acc-2", "ref-a")
        _insert_account(conn, "acc-3", "  ref-a  ")

    with _db(db_path) as conn:
        assert _refs(conn) == ["ref-a", "ref-a", "  ref-a  "]


def test_reupgrade_recreates_index_and_enforcement(tmp_path):
    db_path = tmp_path / "reupgrade.db"
    _upgrade(db_path)
    _run_alembic_cmd(db_path, "downgrade", ["-1"])

    with _db(db_path) as conn:
        _insert_account(conn, "acc-1", "ref-a")
        _insert_account(conn, "acc-2", "ref-b")

    _upgrade(db_path)

    with _db(db_path) as conn:
        assert _index_sql(conn, INDEX_NAME) == EXPECTED_INDEX_SQL
        assert _refs(conn) == ["ref-a", "ref-b"]

    with pytest.raises(IntegrityError):
        with _db(db_path) as conn:
            _insert_account(conn, "acc-3", " ref-a ")

    with _db(db_path) as conn:
        assert _refs(conn) == ["ref-a", "ref-b"]
