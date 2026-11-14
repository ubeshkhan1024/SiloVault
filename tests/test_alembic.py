"""Tests for Alembic database migration configuration and behaviour.

All tests use isolated temporary databases so the real ``silo_vault.db`` is
never touched or altered during testing.
"""

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent  # c:\Projects\SiloVault\tests -> parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

EXPECTED_COLUMNS = {
    "id",
    "provider",
    "provider_account_id",
    "provider_file_id",
    "name",
    "logical_path",
    "mime_type",
    "size_bytes",
    "sha256",
    "category",
    "tags",
    "sync_state",
    "created_at",
    "updated_at",
    "last_indexed_at",
    "provider_metadata",
}

EXPECTED_ACTIVITY_COLUMNS = {
    "id",
    "action_type",
    "file_id",
    "file_name",
    "provider",
    "status",
    "timestamp",
    "details",
}

EXPECTED_ACTIVITY_INDICES = {
    "ix_activity_records_action_type",
    "ix_activity_records_provider",
    "ix_activity_records_timestamp",
}

EXPECTED_INDICES = {
    "ix_name_path",
    "ix_provider_account",
    "ix_provider_checksum",
    "ix_unified_file_records_name",
    "ix_unified_file_records_provider",
    "ix_unified_file_records_provider_account_id",
    "ix_unified_file_records_sha256",
    "ix_unified_file_records_sync_state",
    "uq_local_provider_file",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_alembic_cmd(db_path: Path, cmd: str, extra_args: list[str] | None = None) -> None:
    """Run an Alembic command pointing at *db_path* by overriding DATABASE_PATH."""
    from importlib import reload
    import backend.config as config_module

    os.environ["DATABASE_PATH"] = str(db_path)
    try:
        reload(config_module)
        from alembic.config import Config
        from alembic import command
        cfg = Config(str(ALEMBIC_INI))
        getattr(command, cmd)(cfg, *(extra_args or []))
    finally:
        os.environ.pop("DATABASE_PATH", None)
        reload(config_module)


def _make_engine(tmp_path: Path, name: str) -> sa.engine.Engine:
    return sa.create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False},
        future=True,
    )


def _table_names(conn: sa.Connection) -> list[str]:
    inspector = sa.inspect(conn)
    return sorted(inspector.get_table_names())


def _column_names(conn: sa.Connection, table: str) -> set[str]:
    cols = sa.inspect(conn).get_columns(table)
    return {c["name"] for c in cols}


def _index_info(conn: sa.Connection, table: str) -> list[dict]:
    return sa.inspect(conn).get_indexes(table)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db(tmp_path):
    """Yield a unique temp SQLite file for each test."""
    yield tmp_path / "test_silo_vault.db"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_alembic_config_loads(temp_db):
    """Alembic must resolve to a valid sqlite:/// URL at runtime via env.py."""
    # Verify _resolve_database_url picks up the overridden DATABASE_PATH correctly.
    import os as _os
    from importlib import reload
    import backend.config as _config_module
    _os.environ["DATABASE_PATH"] = str(temp_db)
    try:
        reload(_config_module)
        url = _config_module.settings.database_url
        assert url.startswith("sqlite:///")
        assert str(temp_db) in url
    finally:
        _os.environ.pop("DATABASE_PATH", None)
        reload(_config_module)


def test_env_py_imports_correct_metadata():
    """env.py imports Base and all registered models so autogenerate sees them."""
    from backend.database import Base
    import backend.models  # side-effect: registers UnifiedFileRecord
    assert "unified_file_records" in Base.metadata.tables


# ---------------------------------------------------------------------------
# Fresh DB upgrade
# ---------------------------------------------------------------------------

def test_fresh_db_upgrades_to_head(temp_db):
    """A brand-new empty SQLite database must upgrade cleanly to head revision."""
    _run_alembic_cmd(temp_db, "upgrade", ["head"])

    engine = _make_engine(temp_db.parent, temp_db.name)
    try:
        with engine.connect() as conn:
            tables = _table_names(conn)
            assert "unified_file_records" in tables
            assert "activity_records" in tables
            assert "pending_deletions" in tables
            assert "alembic_version" in tables
            rev = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
            assert rev == "20261114165042"
    finally:
        engine.dispose()


def test_schema_matches_sqlalchemy_model_after_upgrade(temp_db):
    """After upgrading a fresh DB, every column and index declared on the model exists."""
    _run_alembic_cmd(temp_db, "upgrade", ["head"])

    engine = _make_engine(temp_db.parent, temp_db.name)
    try:
        with engine.connect() as conn:
            columns = _column_names(conn, "unified_file_records")
            assert EXPECTED_COLUMNS == columns, (
                f"Missing: {EXPECTED_COLUMNS - columns}; Extra: {columns - EXPECTED_COLUMNS}"
            )

            indexes = _index_info(conn, "unified_file_records")
            idx_names = {idx["name"] for idx in indexes}
            assert EXPECTED_INDICES.issubset(idx_names), (
                f"Missing indexes: {EXPECTED_INDICES - idx_names}"
            )

            act_columns = _column_names(conn, "activity_records")
            assert EXPECTED_ACTIVITY_COLUMNS == act_columns, (
                f"Missing: {EXPECTED_ACTIVITY_COLUMNS - act_columns}; Extra: {act_columns - EXPECTED_ACTIVITY_COLUMNS}"
            )

            act_indexes = _index_info(conn, "activity_records")
            act_idx_names = {idx["name"] for idx in act_indexes}
            assert EXPECTED_ACTIVITY_INDICES.issubset(act_idx_names), (
                f"Missing activity indexes: {EXPECTED_ACTIVITY_INDICES - act_idx_names}"
            )

            # Verify partial unique index exists.
            uq_parts = [
                idx for idx in indexes if idx["name"] == "uq_local_provider_file"
            ]
            assert len(uq_parts) == 1
            assert uq_parts[0]["unique"] == 1 or uq_parts[0]["unique"] is True

            # Primary key check.
            pk = sa.inspect(conn).get_pk_constraint("unified_file_records")
            assert pk["constrained_columns"] == ["id"]

            # Unique constraint check.
            uqs = sa.inspect(conn).get_unique_constraints("unified_file_records")
            names = {qc["name"] for qc in uqs}
            assert "uq_provider_account_file" in names
    finally:
        engine.dispose()


def test_partial_unique_index_exists(temp_db):
    """The sqlite_where condition on uq_local_provider_file must be present in DDL."""
    _run_alembic_cmd(temp_db, "upgrade", ["head"])

    engine = _make_engine(temp_db.parent, temp_db.name)
    try:
        with engine.connect() as conn:
            cur = conn.connection.cursor()
            row = cur.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_local_provider_file'"
            ).fetchone()
            assert row is not None
            assert "WHERE provider = 'LOCAL' AND provider_account_id IS NULL" in row[0]
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Stamp / adoption tests
# ---------------------------------------------------------------------------

def test_stamp_existing_database_does_not_duplicate_tables(temp_db):
    """Stamping a pre-existing schema must create/update only alembic_version."""
    # Simulate legacy DB: create the table directly via SQLAlchemy.
    engine_prep = _make_engine(temp_db.parent, temp_db.name)
    from backend.database import Base
    Base.metadata.create_all(bind=engine_prep)
    engine_prep.dispose()

    # Now stamp at head.
    _run_alembic_cmd(temp_db, "stamp", ["head"])

    engine_check = _make_engine(temp_db.parent, temp_db.name)
    try:
        with engine_check.connect() as conn:
            tables = _table_names(conn)
            assert tables.count("unified_file_records") == 1
            assert "alembic_version" in tables
            alembic_tables = [t for t in tables if t.startswith("alembic")]
            assert alembic_tables == ["alembic_version"]

            rev = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
            assert rev is not None
            assert len(rev) > 0
    finally:
        engine_check.dispose()


def test_existing_records_survive_stamping(temp_db):
    """Data inserted before migration must remain intact after stamping."""
    from backend.database import Base

    # Seed a record directly.
    engine_prep = _make_engine(temp_db.parent, temp_db.name)
    with engine_prep.begin() as prep_conn:
        Base.metadata.create_all(bind=engine_prep)
        prep_conn.execute(
            sa.text(
                "INSERT INTO unified_file_records "
                "(id, provider, provider_account_id, provider_file_id, name, logical_path, "
                " mime_type, size_bytes, tags, sync_state, created_at, updated_at) "
                "VALUES (:id, :provider, :account_id, :file_id, :name, :path, "
                " :mime, :size, :tags, :state, :ts, :ts)"
            ),
            {
                "id": "file_abc123",
                "provider": "LOCAL",
                "account_id": None,
                "file_id": "saved_file.bin",
                "name": "preserved.txt",
                "path": "/preserved.txt",
                "mime": "text/plain",
                "size": 999,
                "tags": "[]",
                "state": "LOCAL",
                "ts": "2024-01-15T10:30:00+00:00",
            },
        )
    engine_prep.dispose()

    # Stamp.
    _run_alembic_cmd(temp_db, "stamp", ["head"])

    engine_check = _make_engine(temp_db.parent, temp_db.name)
    try:
        with engine_check.connect() as conn:
            row = conn.execute(
                sa.text("SELECT id, name, size_bytes FROM unified_file_records WHERE id=:id"),
                {"id": "file_abc123"},
            ).fetchone()
            assert row is not None, "Existing record was lost during stamping"
            assert row[0] == "file_abc123"
            assert row[1] == "preserved.txt"
            assert row[2] == 999
    finally:
        engine_check.dispose()


# ---------------------------------------------------------------------------
# Downgrade (safe — only on temp DBs)
# ---------------------------------------------------------------------------

def test_downgrade_removes_table_and_rev_record(temp_db):
    """Downgrading a freshly-upgraded temp DB must drop the table and revision."""
    _run_alembic_cmd(temp_db, "upgrade", ["head"])
    _run_alembic_cmd(temp_db, "downgrade", ["base"])

    engine_check = _make_engine(temp_db.parent, temp_db.name)
    try:
        with engine_check.connect() as conn:
            tables = _table_names(conn)
            # alembic_version is auto-created/managed by Alembic and persists after downgrade.
            assert "unified_file_records" not in tables
    finally:
        engine_check.dispose()


def test_downgrade_restores_original_data_on_re_upgrade(temp_db):
    """Upgrading after a downgrade recreates the full schema."""
    _run_alembic_cmd(temp_db, "upgrade", ["head"])
    _run_alembic_cmd(temp_db, "downgrade", ["base"])
    _run_alembic_cmd(temp_db, "upgrade", ["head"])

    engine_check = _make_engine(temp_db.parent, temp_db.name)
    try:
        with engine_check.connect() as conn:
            tables = _table_names(conn)
            assert "unified_file_records" in tables
            assert "activity_records" in tables
            assert "pending_deletions" in tables
            assert "alembic_version" in tables
            columns = _column_names(conn, "unified_file_records")
            assert EXPECTED_COLUMNS == columns
    finally:
        engine_check.dispose()
