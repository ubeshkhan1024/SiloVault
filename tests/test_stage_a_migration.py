import os
import uuid
import subprocess
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from backend.models.pending_deletion import PendingDeletion
from backend.database import Base

def _run_alembic(db_path: str, command: str, revision: str = "head"):
    """Run alembic against a specific database path."""
    env = os.environ.copy()
    env["DATABASE_PATH"] = db_path
    cmd = ["alembic", command, revision]
    subprocess.check_call(cmd, env=env, stderr=subprocess.PIPE)

@pytest.fixture
def tmp_db():
    """Fixture to create and clean up a temporary SQLite database."""
    db_name = f"test_migration_{uuid.uuid4().hex}.db"
    db_path = os.path.abspath(db_name)
    yield db_path
    # Cleanup WAL/SHM first, then DB
    for ext in ["-wal", "-shm", ""]:
        if os.path.exists(db_path + ext):
            try:
                os.remove(db_path + ext)
            except PermissionError:
                # File may be locked by Alembic, retry
                import time
                time.sleep(0.1)
                try:
                    os.remove(db_path + ext)
                except PermissionError:
                    pass

def test_upgrade_adds_all_columns(tmp_db):
    """Verify that upgrading to head adds all expected columns."""
    _run_alembic(tmp_db, "upgrade")
    
    engine = create_engine(f"sqlite:///{tmp_db}")
    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("pending_deletions")}
    
    # Check all new columns
    expected_new = [
        "original_file_id", "original_logical_path", "original_mime_type",
        "original_size_bytes", "original_sha256", "original_category",
        "original_tags", "operation_generation"
    ]
    for col in expected_new:
        assert col in columns, f"Column {col} missing"
        
    # Check status length
    assert columns["status"]["type"].length == 32
    
    # Check operation_generation default and nullability
    assert columns["operation_generation"]["nullable"] is False
    # server_default is '0' in SQLite
    assert columns["operation_generation"]["default"] == "0"

def test_downgrade_removes_columns_and_shrinks_status(tmp_db):
    """Verify that downgrading removes new columns and restores status length."""
    _run_alembic(tmp_db, "upgrade")
    _run_alembic(tmp_db, "downgrade", "20261114165042")
    
    engine = create_engine(f"sqlite:///{tmp_db}")
    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("pending_deletions")}
    
    removed = [
        "original_file_id", "original_logical_path", "original_mime_type",
        "original_size_bytes", "original_sha256", "original_category",
        "original_tags", "operation_generation"
    ]
    for col in removed:
        assert col not in columns, f"Column {col} should have been removed"
        
    assert columns["status"]["type"].length == 16

def test_historical_rows_survive_upgrade(tmp_db):
    """Verify that existing rows are preserved and receive correct defaults."""
    # 1. Upgrade to the revision BEFORE Stage A
    _run_alembic(tmp_db, "upgrade", "20261114165042")
    
    # 2. Insert a historical row
    engine = create_engine(f"sqlite:///{tmp_db}")
    Session = sessionmaker(bind=engine)
    with Session() as session:
        # Use a raw SQL insert to avoid model mismatch before upgrade
        session.execute(text(
            "INSERT INTO pending_deletions (id, provider, provider_file_id, quarantine_id, file_name_snapshot, status, retry_count, created_at) "
            "VALUES (:id, :p, :pf, :q, :fn, :s, :r, :c)"
        ), {
            "id": "del_hist_1", "p": "LOCAL", "pf": "old_file", "q": "q_old", "fn": "old.txt", "s": "PENDING", "r": 0, "c": "2026-11-10 00:00:00"
        })
        session.commit()
        
    # 3. Upgrade to head (Stage A)
    _run_alembic(tmp_db, "upgrade")
    
    # 4. Verify the row survived and has NULL snapshots + generation 0
    with Session() as session:
        row = session.execute(text("SELECT * FROM pending_deletions WHERE id = 'del_hist_1'")).mappings().one()
        assert row["provider_file_id"] == "old_file"
        assert row["original_file_id"] is None
        assert row["original_tags"] is None
        assert row["operation_generation"] == 0

def test_upgrade_downgrade_reupgrade_cycle(tmp_db):
    """Verify that the schema can be repeatedly upgraded and downgraded."""
    # Cycle 1: Upgrade
    _run_alembic(tmp_db, "upgrade")
    engine = create_engine(f"sqlite:///{tmp_db}")
    inspector = inspect(engine)
    assert "original_file_id" in [c["name"] for c in inspector.get_columns("pending_deletions")]
    
    # Cycle 2: Downgrade
    _run_alembic(tmp_db, "downgrade", "20261114165042")
    inspector = inspect(engine)
    assert "original_file_id" not in [c["name"] for c in inspector.get_columns("pending_deletions")]
    
    # Cycle 3: Re-upgrade
    _run_alembic(tmp_db, "upgrade")
    inspector = inspect(engine)
    assert "original_file_id" in [c["name"] for c in inspector.get_columns("pending_deletions")]
    cols = {c["name"]: c for c in inspector.get_columns("pending_deletions")}
    assert cols["operation_generation"]["nullable"] is False
    assert cols["operation_generation"]["default"] == "0"
