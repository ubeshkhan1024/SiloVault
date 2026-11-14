"""Focused tests for the AI task database foundation (M4.1 Stage 1A).

Covers two layers:

1. **Model layer** — exercised against an isolated in-memory SQLite database
   built from ``Base.metadata``. Verifies defaults, the nullable
   ``file_id`` with ``ON DELETE SET NULL``, multiple tasks per file, and the
   closed status set.

2. **Migration layer** — exercised against an isolated temporary SQLite file
   (never the development database) by driving Alembic through
   ``DATABASE_PATH`` overrides, mirroring :mod:`tests.test_alembic`. Verifies
   that upgrading to head creates ``ai_tasks`` with the exact columns/indexes
   the model declares, and that downgrading removes only the AI task objects.

No AI worker, API, runtime, or inference is exercised here.
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.ai_task import AiTask
from backend.models.unified_file import UnifiedFileRecord

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

# Expected head revision after the AI tasks migration is applied.
EXPECTED_AI_HEAD = "20261114165042"

# Columns the model declares on ai_tasks.
EXPECTED_AI_COLUMNS = {
    "id",
    "file_id",
    "task_type",
    "status",
    "retries",
    "max_retries",
    "created_at",
    "started_at",
    "completed_at",
    "cancellation_requested_at",
    "claim_token",
    "worker_id",
    "lease_expires_at",
    "proposal_json",
    "error_message",
}

EXPECTED_AI_INDEXES = {
    "ix_ai_tasks_status",
    "ix_ai_tasks_lease_expires",
    "ix_ai_tasks_file_id",
}

# Closed status state machine.
VALID_STATUSES = {
    "PENDING",
    "PROCESSING",
    "AWAITING_APPROVAL",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_alembic_cmd(db_path: Path, cmd: str, extra_args: list[str] | None = None) -> None:
    """Run an Alembic command pointing at *db_path* via a DATABASE_PATH override."""
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


def _column_names(conn: sa.Connection, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns(table)}


def _index_names(conn: sa.Connection, table: str) -> set[str]:
    return {idx["name"] for idx in sa.inspect(conn).get_indexes(table)}


def _fk_info(conn: sa.Connection, table: str) -> list[dict]:
    return sa.inspect(conn).get_foreign_keys(table)


# ---------------------------------------------------------------------------
# Model-layer fixtures (in-memory)
# ---------------------------------------------------------------------------

@pytest.fixture
def mem_session():
    """In-memory SQLite session with foreign keys enforced."""
    engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_file(session, name: str = "doc.txt") -> UnifiedFileRecord:
    record = UnifiedFileRecord(
        provider="LOCAL",
        provider_account_id=None,
        provider_file_id=name,
        name=name,
        logical_path=f"/{name}",
        mime_type="text/plain",
        size_bytes=42,
        sync_state="LOCAL",
    )
    session.add(record)
    session.commit()
    return record


# ---------------------------------------------------------------------------
# Model-layer tests
# ---------------------------------------------------------------------------

def test_task_defaults(mem_session):
    """A newly created task defaults to PENDING with zero retries."""
    task = AiTask(task_type="text_classification")
    mem_session.add(task)
    mem_session.commit()

    assert task.status == "PENDING"
    assert task.id.startswith("task_")
    assert task.retries == 0
    assert task.max_retries == 3
    assert task.created_at is not None
    assert task.file_id is None
    assert task.claim_token is None
    assert task.worker_id is None
    assert task.lease_expires_at is None
    assert task.proposal_json is None
    assert task.error_message is None


def test_multiple_tasks_per_file(mem_session):
    """Several distinct tasks may reference the same file (no unique file_id)."""
    file = _make_file(mem_session)

    t1 = AiTask(file_id=file.id, task_type="text_classification")
    t2 = AiTask(file_id=file.id, task_type="text_classification")
    mem_session.add_all([t1, t2])
    mem_session.commit()

    rows = mem_session.query(AiTask).filter(AiTask.file_id == file.id).all()
    assert len(rows) == 2
    assert {r.id for r in rows} == {t1.id, t2.id}


def test_file_deletion_sets_file_id_null(mem_session):
    """Deleting the referenced file nulls file_id but preserves the task row."""
    file = _make_file(mem_session)
    task = AiTask(file_id=file.id, task_type="text_classification")
    mem_session.add(task)
    mem_session.commit()

    mem_session.delete(file)
    mem_session.commit()

    task = mem_session.get(AiTask, task.id)
    assert task is not None, "Task must survive deletion of its file"
    assert task.file_id is None, "file_id must be SET NULL on file deletion"


def test_all_valid_statuses_storable(mem_session):
    """Every status in the closed state machine can be persisted."""
    for status in VALID_STATUSES:
        task = AiTask(task_type="text_classification", status=status)
        mem_session.add(task)
    mem_session.commit()

    stored = {t.status for t in mem_session.query(AiTask).all()}
    assert stored == VALID_STATUSES


def test_claim_and_retry_fields(mem_session):
    """Claim, lease and retry fields accept and persist their values."""
    task = AiTask(
        task_type="text_classification",
        status="PROCESSING",
        claim_token="tok_123",
        worker_id="worker-1",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        retries=1,
    )
    mem_session.add(task)
    mem_session.commit()

    loaded = mem_session.get(AiTask, task.id)
    assert loaded.claim_token == "tok_123"
    assert loaded.worker_id == "worker-1"
    assert loaded.retries == 1
    assert loaded.lease_expires_at is not None
    assert loaded.lease_expires_at.tzinfo is not None  # UTC-aware


def test_proposal_json_roundtrip(mem_session):
    """proposal_json stores AI metadata as a proposal without touching the file."""
    file = _make_file(mem_session)
    task = AiTask(
        file_id=file.id,
        task_type="text_classification",
        proposal_json={"tags": ["#receipt"], "category": "Financial"},
    )
    mem_session.add(task)
    mem_session.commit()

    loaded = mem_session.get(AiTask, task.id)
    assert loaded.proposal_json == {"tags": ["#receipt"], "category": "Financial"}

    # The original file metadata must be untouched by the proposal.
    file = mem_session.get(UnifiedFileRecord, file.id)
    assert file.tags == []
    assert file.category is None


# ---------------------------------------------------------------------------
# Migration-layer tests (isolated temp DB)
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db(tmp_path):
    yield tmp_path / "test_ai_tasks.db"


def test_upgrade_creates_ai_tasks_table(temp_db):
    """Upgrading a fresh DB to head creates ai_tasks alongside existing tables."""
    _run_alembic_cmd(temp_db, "upgrade", ["head"])

    engine = _make_engine(temp_db.parent, temp_db.name)
    try:
        with engine.connect() as conn:
            tables = sa.inspect(conn).get_table_names()
            assert "ai_tasks" in tables
            # Existing tables still present.
            assert "unified_file_records" in tables
            assert "activity_records" in tables
            assert "pending_deletions" in tables
            rev = conn.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar()
            assert rev == EXPECTED_AI_HEAD
    finally:
        engine.dispose()


def test_ai_tasks_schema_matches_model(temp_db):
    """After upgrade, ai_tasks columns and indexes match the model exactly."""
    _run_alembic_cmd(temp_db, "upgrade", ["head"])

    engine = _make_engine(temp_db.parent, temp_db.name)
    try:
        with engine.connect() as conn:
            columns = _column_names(conn, "ai_tasks")
            assert columns == EXPECTED_AI_COLUMNS, (
                f"Missing: {EXPECTED_AI_COLUMNS - columns}; "
                f"Extra: {columns - EXPECTED_AI_COLUMNS}"
            )

            indexes = _index_names(conn, "ai_tasks")
            assert EXPECTED_AI_INDEXES.issubset(indexes), (
                f"Missing indexes: {EXPECTED_AI_INDEXES - indexes}"
            )

            # file_id must be nullable.
            cols = {c["name"]: c for c in sa.inspect(conn).get_columns("ai_tasks")}
            assert cols["file_id"]["nullable"] is True
            assert cols["status"]["nullable"] is False
            assert cols["retries"]["nullable"] is False
            assert cols["max_retries"]["nullable"] is False

            # Primary key is id.
            pk = sa.inspect(conn).get_pk_constraint("ai_tasks")
            assert pk["constrained_columns"] == ["id"]
    finally:
        engine.dispose()


def test_file_id_foreign_key_targets_unified_file_records(temp_db):
    """The file_id FK references unified_file_records.id with SET NULL."""
    _run_alembic_cmd(temp_db, "upgrade", ["head"])

    engine = _make_engine(temp_db.parent, temp_db.name)
    try:
        with engine.connect() as conn:
            fks = _fk_info(conn, "ai_tasks")
            assert len(fks) == 1, f"Expected exactly one FK, got {fks}"
            fk = fks[0]
            assert fk["constrained_columns"] == ["file_id"]
            assert fk["referred_table"] == "unified_file_records"
            assert fk["referred_columns"] == ["id"]
            assert fk["options"].get("ondelete") == "SET NULL"
    finally:
        engine.dispose()


def test_downgrade_removes_only_ai_tasks(temp_db):
    """Downgrading one step drops ai_tasks and leaves all other tables intact."""
    _run_alembic_cmd(temp_db, "upgrade", ["head"])
    _run_alembic_cmd(temp_db, "downgrade", ["-1"])

    engine = _make_engine(temp_db.parent, temp_db.name)
    try:
        with engine.connect() as conn:
            tables = sa.inspect(conn).get_table_names()
            assert "ai_tasks" not in tables
            # Everything else survives.
            assert "unified_file_records" in tables
            assert "activity_records" in tables
            assert "pending_deletions" in tables
            rev = conn.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar()
            assert rev == "597b2cc67a4a"
    finally:
        engine.dispose()


def test_upgrade_downgrade_upgrade_roundtrip(temp_db):
    """ai_tasks can be dropped and recreated cleanly."""
    _run_alembic_cmd(temp_db, "upgrade", ["head"])
    _run_alembic_cmd(temp_db, "downgrade", ["-1"])
    _run_alembic_cmd(temp_db, "upgrade", ["head"])

    engine = _make_engine(temp_db.parent, temp_db.name)
    try:
        with engine.connect() as conn:
            columns = _column_names(conn, "ai_tasks")
            assert columns == EXPECTED_AI_COLUMNS
            rev = conn.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar()
            assert rev == EXPECTED_AI_HEAD
    finally:
        engine.dispose()
