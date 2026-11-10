"""Tests for the SQLite database foundation.

These tests exercise the SQLAlchemy engine, session factory, database
initialization helper, and the FastAPI ``get_db`` dependency directly. They
deliberately avoid ``fastapi.testclient.TestClient`` so they remain fast and
deterministic even when the broader request-level test suite has issues.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.config import settings
from backend.database import Base, SessionLocal, engine, get_db, init_db
from backend.models.unified_file import UnifiedFileRecord


def test_database_url_uses_local_sqlite():
    """The configured database URL must point at a local SQLite file."""
    assert settings.database_url.startswith("sqlite:///")


def test_engine_is_bound_to_configured_database():
    """The shared engine must reflect the configured database URL."""
    assert engine.url.render_as_string() == settings.database_url


def test_session_factory_yields_independent_sessions():
    """Each call to the session factory must produce an independent session."""
    session_a = SessionLocal()
    session_b = SessionLocal()
    try:
        assert session_a is not session_b
    finally:
        session_a.close()
        session_b.close()


def test_init_db_creates_database_file():
    """``init_db`` must create the configured database file on disk."""
    init_db()

    db_path = Path(settings.database_path)
    assert db_path.exists(), f"Expected database file at {db_path}"


def test_init_db_is_idempotent():
    """Calling ``init_db`` twice must not raise and must not drop data."""
    init_db()
    init_db()  # second call should be a no-op


def test_base_metadata_is_registered():
    """``Base.metadata`` must be a real SQLAlchemy MetaData instance."""
    assert Base.metadata is not None
    assert hasattr(Base.metadata, "create_all")


def test_get_db_dependency_closes_session_on_exception():
    """A session yielded by ``get_db`` must be closed even on exceptions.

    Drives the dependency generator with ``.throw()`` so the exception is
    raised *inside* the generator, exercising its ``finally`` block.
    """
    closed = {"value": False}
    original_close = SessionLocal().close

    def tracking_close(self):
        closed["value"] = True
        original_close()

    from sqlalchemy.orm import Session as _Session

    _Session.close = tracking_close  # type: ignore[assignment]
    try:
        dependency = get_db()
        session = next(dependency)  # enters generator, yields session
        assert session is not None

        with pytest.raises(RuntimeError):
            dependency.throw(RuntimeError("simulated request failure"))
    finally:
        _Session.close = original_close  # type: ignore[assignment]

    assert closed["value"] is True


def test_get_db_dependency_closes_session_normally():
    """A session yielded by ``get_db`` must be closed on normal completion."""
    closed = {"value": False}
    original_close = SessionLocal().close

    def tracking_close(self):
        closed["value"] = True
        original_close()

    from sqlalchemy.orm import Session as _Session

    _Session.close = tracking_close  # type: ignore[assignment]
    try:
        dependency = get_db()
        session = next(dependency)  # enters generator, yields session
        assert session is not None

        with pytest.raises(StopIteration):
            next(dependency)  # completes generator, runs finally
    finally:
        _Session.close = original_close  # type: ignore[assignment]

    assert closed["value"] is True


def test_database_file_is_ignored_by_default_gitignore():
    """The configured database path must be excluded from version control."""
    gitignore = Path(".gitignore")
    assert gitignore.exists()
    content = gitignore.read_text(encoding="utf-8")
    assert "*.db" in content


def test_engine_url_does_not_expose_network():
    """The database URL must not reference any network host."""
    assert "://" not in settings.database_url.replace("sqlite:///", "", 1)
    assert "@" not in settings.database_url


def test_inspect_reports_unified_file_table():
    """After models are registered, the unified_file_records table exists."""
    init_db()
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "unified_file_records" in tables
    # Verify columns exist
    columns = {c["name"] for c in inspector.get_columns("unified_file_records")}
    expected_columns = {
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
    assert expected_columns.issubset(columns)


# ---------------------------------------------------------------------------
# Model-Level Tests for UnifiedFileRecord (Isolated In-Memory Database)
# ---------------------------------------------------------------------------

@pytest.fixture
def memory_db_session():
    """Yields an isolated, clean SQLite session for each model test."""
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(bind=test_engine)
    Session = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        test_engine.dispose()


def test_unified_file_record_id_format_and_length(memory_db_session):
    """Generated primary keys must follow the file_<UUID> format and be 41 chars."""
    record = UnifiedFileRecord(
        provider="LOCAL",
        provider_file_id="/path/to/doc.pdf",
        name="doc.pdf",
        logical_path="/docs/doc.pdf",
        mime_type="application/pdf",
        size_bytes=2048,
    )
    memory_db_session.add(record)
    memory_db_session.commit()

    assert record.id.startswith("file_")
    assert len(record.id) == 41


def test_unified_file_record_crud(memory_db_session):
    """Records can be persisted and retrieved accurately."""
    record = UnifiedFileRecord(
        provider="GOOGLE_DRIVE",
        provider_account_id="acc_123",
        provider_file_id="gdrive_file_999",
        name="report.docx",
        logical_path="/Work/report.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=10240,
        sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        category="Documents",
        sync_state="INDEXED",
    )
    memory_db_session.add(record)
    memory_db_session.commit()

    retrieved = memory_db_session.get(UnifiedFileRecord, record.id)
    assert retrieved is not None
    assert retrieved.name == "report.docx"
    assert retrieved.provider == "GOOGLE_DRIVE"
    assert retrieved.provider_account_id == "acc_123"
    assert retrieved.sync_state == "INDEXED"


def test_unified_file_record_tags_serialization(memory_db_session):
    """Tags list serializes to JSON and deserializes back to a Python list."""
    record = UnifiedFileRecord(
        provider="LOCAL",
        provider_file_id="file_1",
        name="photo.jpg",
        logical_path="/photos/photo.jpg",
        mime_type="image/jpeg",
        size_bytes=4096,
        tags=["vacation", "2026", "beach"],
    )
    memory_db_session.add(record)
    memory_db_session.commit()

    retrieved = memory_db_session.get(UnifiedFileRecord, record.id)
    assert retrieved is not None
    assert isinstance(retrieved.tags, list)
    assert retrieved.tags == ["vacation", "2026", "beach"]


def test_unified_file_record_provider_metadata_serialization(memory_db_session):
    """Provider metadata serializes to JSON and deserializes back to a dict."""
    metadata = {
        "web_view_link": "https://drive.google.com/file/d/123",
        "version": 4,
        "shared": True,
    }
    record = UnifiedFileRecord(
        provider="GOOGLE_DRIVE",
        provider_account_id="acc_456",
        provider_file_id="gfile_abc",
        name="sheet.xlsx",
        logical_path="/Finance/sheet.xlsx",
        mime_type="application/vnd.ms-excel",
        size_bytes=8192,
        provider_metadata=metadata,
    )
    memory_db_session.add(record)
    memory_db_session.commit()

    retrieved = memory_db_session.get(UnifiedFileRecord, record.id)
    assert retrieved is not None
    assert isinstance(retrieved.provider_metadata, dict)
    assert retrieved.provider_metadata == metadata


def test_unified_file_record_retrieved_timestamps_are_utc_aware(memory_db_session):
    """Retrieved created_at, updated_at, and last_indexed_at timestamps are UTC-aware."""
    now_utc = datetime.now(timezone.utc)
    record = UnifiedFileRecord(
        provider="DROPBOX",
        provider_account_id="db_user_1",
        provider_file_id="id:12345",
        name="data.csv",
        logical_path="/data.csv",
        mime_type="text/csv",
        size_bytes=512,
        created_at=now_utc,
        updated_at=now_utc,
        last_indexed_at=now_utc,
    )
    memory_db_session.add(record)
    memory_db_session.commit()

    retrieved = memory_db_session.get(UnifiedFileRecord, record.id)
    assert retrieved is not None
    assert retrieved.created_at.tzinfo == timezone.utc
    assert retrieved.updated_at.tzinfo == timezone.utc
    assert retrieved.last_indexed_at is not None
    assert retrieved.last_indexed_at.tzinfo == timezone.utc


def test_unified_file_record_naive_datetime_treated_as_utc(memory_db_session):
    """Naive datetime inputs are converted to UTC-aware datetimes."""
    naive_dt = datetime(2026, 11, 10, 12, 0, 0)
    record = UnifiedFileRecord(
        provider="LOCAL",
        provider_file_id="local_file_naive",
        name="test.txt",
        logical_path="/test.txt",
        mime_type="text/plain",
        size_bytes=100,
        created_at=naive_dt,
    )
    memory_db_session.add(record)
    memory_db_session.commit()

    retrieved = memory_db_session.get(UnifiedFileRecord, record.id)
    assert retrieved is not None
    assert retrieved.created_at.tzinfo == timezone.utc
    assert retrieved.created_at.year == 2026
    assert retrieved.created_at.hour == 12


def test_unified_file_record_rejects_duplicate_local_records_with_null_account(memory_db_session):
    """Duplicate LOCAL records with NULL provider_account_id must raise IntegrityError."""
    record1 = UnifiedFileRecord(
        provider="LOCAL",
        provider_account_id=None,
        provider_file_id="C:/data/file1.txt",
        name="file1.txt",
        logical_path="/data/file1.txt",
        mime_type="text/plain",
        size_bytes=100,
    )
    record2 = UnifiedFileRecord(
        provider="LOCAL",
        provider_account_id=None,
        provider_file_id="C:/data/file1.txt",
        name="file1.txt",
        logical_path="/data/file1.txt",
        mime_type="text/plain",
        size_bytes=100,
    )
    memory_db_session.add(record1)
    memory_db_session.commit()

    memory_db_session.add(record2)
    with pytest.raises(IntegrityError):
        memory_db_session.commit()


def test_unified_file_record_rejects_duplicate_cloud_records(memory_db_session):
    """Duplicate cloud provider/account/file combinations must raise IntegrityError."""
    record1 = UnifiedFileRecord(
        provider="BOX",
        provider_account_id="box_user_A",
        provider_file_id="file_100",
        name="notes.txt",
        logical_path="/notes.txt",
        mime_type="text/plain",
        size_bytes=500,
    )
    record2 = UnifiedFileRecord(
        provider="BOX",
        provider_account_id="box_user_A",
        provider_file_id="file_100",
        name="notes.txt",
        logical_path="/notes.txt",
        mime_type="text/plain",
        size_bytes=500,
    )
    memory_db_session.add(record1)
    memory_db_session.commit()

    memory_db_session.add(record2)
    with pytest.raises(IntegrityError):
        memory_db_session.commit()


def test_unified_file_record_allows_same_file_id_in_different_cloud_accounts(memory_db_session):
    """Different accounts under the same cloud provider can hold files with the same provider_file_id."""
    record1 = UnifiedFileRecord(
        provider="ONEDRIVE",
        provider_account_id="user_1",
        provider_file_id="shared_file_id",
        name="doc.pdf",
        logical_path="/doc.pdf",
        mime_type="application/pdf",
        size_bytes=1000,
    )
    record2 = UnifiedFileRecord(
        provider="ONEDRIVE",
        provider_account_id="user_2",
        provider_file_id="shared_file_id",
        name="doc.pdf",
        logical_path="/doc.pdf",
        mime_type="application/pdf",
        size_bytes=1000,
    )
    memory_db_session.add(record1)
    memory_db_session.add(record2)
    memory_db_session.commit()

    assert memory_db_session.query(UnifiedFileRecord).count() == 2


def test_unified_file_record_allows_different_local_file_ids(memory_db_session):
    """Multiple local files with distinct provider_file_id and NULL account can coexist."""
    record1 = UnifiedFileRecord(
        provider="LOCAL",
        provider_account_id=None,
        provider_file_id="C:/dir1/a.txt",
        name="a.txt",
        logical_path="/dir1/a.txt",
        mime_type="text/plain",
        size_bytes=100,
    )
    record2 = UnifiedFileRecord(
        provider="LOCAL",
        provider_account_id=None,
        provider_file_id="C:/dir2/b.txt",
        name="b.txt",
        logical_path="/dir2/b.txt",
        mime_type="text/plain",
        size_bytes=200,
    )
    memory_db_session.add(record1)
    memory_db_session.add(record2)
    memory_db_session.commit()

    assert memory_db_session.query(UnifiedFileRecord).count() == 2
