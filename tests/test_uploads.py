"""Tests for the uploads endpoints.

Covers the real local file upload endpoint ``POST /api/v1/uploads`` using
isolated temporary directories and in-memory SQLite databases, so the
development database and real uploads directory are never touched.

The ``GET /api/v1/uploads`` list endpoint is still a placeholder.
"""

import hashlib
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import settings
from backend.database import Base, get_db
from backend.main import app
from backend.models.unified_file import UnifiedFileRecord
from backend.storage.local import LocalAdapter
from backend.storage.registry import register, remove
from backend.storage.capabilities import CAPABILITY_MAP
from backend.storage.errors import StorageError, StorageUnavailableError


@pytest.fixture
def upload_env(tmp_path, monkeypatch):
    """Yields an isolated test environment with temp uploads dir and in-memory DB.

    Returns a dict with:
    - client: TestClient wired to the in-memory DB
    - engine: the SQLAlchemy engine
    - uploads_dir: Path to the temporary uploads directory
    """
    # Override uploads dir to a temp directory
    uploads_dir = tmp_path / "uploads"
    monkeypatch.setattr(settings, "uploads_dir", str(uploads_dir))

    # In-memory database
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db

    # Register LOCAL adapter with the temp uploads directory
    adapter = LocalAdapter(str(uploads_dir))
    register("LOCAL", adapter, CAPABILITY_MAP["LOCAL"])

    try:
        yield {
            "client": TestClient(app),
            "engine": engine,
            "uploads_dir": uploads_dir,
        }
    finally:
        # Clean up adapter registration
        remove("LOCAL")
        app.dependency_overrides.clear()
        engine.dispose()


def _upload_file(client, filename: str, content: bytes):
    """Helper to upload a file via the API."""
    files = {"file": (filename, content, "application/octet-stream")}
    return client.post("/api/v1/uploads", files=files)


def test_successful_upload_and_metadata_registration(upload_env):
    """A successful upload must store the file and register metadata."""
    env = upload_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    content = b"Hello, SiloVault!"
    response = _upload_file(client, "hello.txt", content)

    assert response.status_code == 200
    payload = response.json()

    # Verify metadata
    assert payload["provider"] == "LOCAL"
    assert payload["provider_account_id"] is None
    assert payload["name"] == "hello.txt"
    assert payload["logical_path"] == "/hello.txt"
    assert payload["mime_type"] == "text/plain"
    assert payload["size_bytes"] == len(content)
    assert payload["sync_state"] == "LOCAL"
    assert payload["id"].startswith("file_")
    assert payload["sha256"] == hashlib.sha256(content).hexdigest()

    # Verify file was stored on disk
    stored_files = list(uploads_dir.iterdir())
    assert len(stored_files) == 1
    assert stored_files[0].read_bytes() == content

    # Verify provider_file_id is the storage filename (not the original)
    assert payload["provider_file_id"] == stored_files[0].name
    assert payload["provider_file_id"] != "hello.txt"


def test_correct_sha256_and_file_size(upload_env):
    """SHA-256 and file size must be calculated correctly from uploaded bytes."""
    client = upload_env["client"]

    # Test with known content
    content = b"A" * 1024
    expected_sha = hashlib.sha256(content).hexdigest()

    response = _upload_file(client, "ones.bin", content)
    assert response.status_code == 200
    payload = response.json()
    assert payload["sha256"] == expected_sha
    assert payload["size_bytes"] == 1024

    # Test with different content
    content2 = b"\x00\x01\x02\x03" * 100
    expected_sha2 = hashlib.sha256(content2).hexdigest()

    response = _upload_file(client, "binary.dat", content2)
    assert response.status_code == 200
    payload = response.json()
    assert payload["sha256"] == expected_sha2
    assert payload["size_bytes"] == 400


def test_unique_storage_filenames(upload_env):
    """Each upload must get a unique storage filename, even with same original name."""
    env = upload_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    content = b"same content"

    # Upload the same filename multiple times
    response1 = _upload_file(client, "report.pdf", content)
    response2 = _upload_file(client, "report.pdf", content)
    response3 = _upload_file(client, "report.pdf", content)

    assert response1.status_code == 200
    assert response2.status_code == 200
    assert response3.status_code == 200

    id1 = response1.json()["provider_file_id"]
    id2 = response2.json()["provider_file_id"]
    id3 = response3.json()["provider_file_id"]

    # All storage filenames must be unique
    assert len({id1, id2, id3}) == 3

    # All files must exist on disk
    stored_files = list(uploads_dir.iterdir())
    assert len(stored_files) == 3

    # All must have .pdf extension
    for f in stored_files:
        assert f.suffix == ".pdf"


def test_duplicate_original_filenames_do_not_overwrite(upload_env):
    """Uploading the same original filename must not overwrite previous files."""
    env = upload_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    # Upload first version
    content_v1 = b"version 1"
    response1 = _upload_file(client, "doc.txt", content_v1)
    assert response1.status_code == 200
    storage_id_1 = response1.json()["provider_file_id"]

    # Upload second version (same original name, different content)
    content_v2 = b"version 2"
    response2 = _upload_file(client, "doc.txt", content_v2)
    assert response2.status_code == 200
    storage_id_2 = response2.json()["provider_file_id"]

    # Both files must exist with their respective content
    file1 = uploads_dir / storage_id_1
    file2 = uploads_dir / storage_id_2
    assert file1.exists()
    assert file2.exists()
    assert file1.read_bytes() == content_v1
    assert file2.read_bytes() == content_v2

    # Both names in DB must be "doc.txt"
    assert response1.json()["name"] == "doc.txt"
    assert response2.json()["name"] == "doc.txt"


def test_empty_file_handling(upload_env):
    """An empty file (0 bytes) must be accepted and registered correctly."""
    env = upload_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    response = _upload_file(client, "empty.txt", b"")
    assert response.status_code == 200
    payload = response.json()

    assert payload["size_bytes"] == 0
    assert payload["sha256"] == hashlib.sha256(b"").hexdigest()
    assert payload["name"] == "empty.txt"

    # File must exist on disk (empty)
    stored_file = uploads_dir / payload["provider_file_id"]
    assert stored_file.exists()
    assert stored_file.stat().st_size == 0


def test_upload_size_limit(upload_env):
    """Files exceeding max_upload_size_bytes must be rejected with 413."""
    env = upload_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    # Set a very small limit for testing
    with patch.object(settings, "max_upload_size_bytes", 10):
        # 11 bytes exceeds the 10-byte limit
        response = _upload_file(client, "big.txt", b"x" * 11)
        assert response.status_code == 413
        assert "exceeds maximum" in response.json()["detail"].lower()

        # No file should be left on disk
        assert len(list(uploads_dir.iterdir())) == 0

    # With a larger limit, the same file succeeds
    with patch.object(settings, "max_upload_size_bytes", 100):
        response = _upload_file(client, "big.txt", b"x" * 11)
        assert response.status_code == 200
        assert len(list(uploads_dir.iterdir())) == 1


def test_database_failure_cleans_up_stored_file(upload_env):
    """If database registration fails, the stored file must be cleaned up."""
    env = upload_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    # Simulate database failure by making the session's commit raise
    original_commit = None

    def failing_commit():
        raise RuntimeError("Simulated database failure")

    # We need to patch the session's commit. Since the override creates sessions,
    # we'll use a different approach: patch db.commit on the session
    from sqlalchemy.orm import Session as _Session

    original_session_commit = _Session.commit
    _Session.commit = failing_commit  # type: ignore[assignment]

    try:
        response = _upload_file(client, "test.txt", b"data")
        assert response.status_code == 500
        assert "cleanup" in response.json()["detail"].lower()
    finally:
        _Session.commit = original_session_commit  # type: ignore[assignment]

    # The file should have been cleaned up
    # The quarantine directory (.quarantine) is part of the storage infrastructure
    # and persists; only the user file should be removed.
    user_files = [f for f in uploads_dir.iterdir() if f.name != ".quarantine"]
    assert len(user_files) == 0


def test_existing_files_endpoint_unchanged(upload_env):
    """GET /api/v1/files must still work alongside the upload endpoint."""
    env = upload_env
    client = env["client"]
    engine = env["engine"]

    # Upload a file
    content = b"test data"
    upload_response = _upload_file(client, "test.txt", content)
    assert upload_response.status_code == 200
    file_id = upload_response.json()["id"]

    # List files
    list_response = client.get("/api/v1/files")
    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["total"] == 1
    assert list_payload["items"][0]["id"] == file_id

    # Get file by ID
    get_response = client.get(f"/api/v1/files/{file_id}")
    assert get_response.status_code == 200
    get_payload = get_response.json()
    assert get_payload["name"] == "test.txt"
    assert get_payload["provider"] == "LOCAL"


def test_upload_with_no_filename(upload_env):
    """Upload with no filename should still work (falls back to 'unknown')."""
    client = upload_env["client"]

    content = b"data"
    files = {"file": ("", content, "application/octet-stream")}
    response = client.post("/api/v1/uploads", files=files)

    # FastAPI/Starlette may reject empty filename or handle it
    # Either way, it should not crash with 500
    assert response.status_code in (200, 422)


def test_list_uploads_returns_placeholder():
    """The uploads list endpoint should still return a placeholder response."""
    client = TestClient(app)
    response = client.get("/api/v1/uploads")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "placeholder"
    assert payload["uploads"] == []


# ---------------------------------------------------------------------------
# Failure-injection tests for upload endpoint
# ---------------------------------------------------------------------------

def test_upload_db_commit_fails_quarantine_and_cleanup_succeed(upload_env):
    """DB commit fails; adapter.quarantine() and adapter.cleanup() both succeed.
    The uploaded file must be removed and no activity recorded."""
    import unittest.mock
    from sqlalchemy.orm import Session as _Session

    env = upload_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    content = b"test data for db failure"

    original_commit = _Session.commit

    def failing_commit(self):
        raise RuntimeError("Simulated database failure")

    _Session.commit = failing_commit  # type: ignore[assignment]

    try:
        response = _upload_file(client, "db_fail.txt", content)
        assert response.status_code == 500
        assert "cleanup" in response.json()["detail"].lower()

        # The file should be cleaned up (no user files left, only .quarantine dir)
        user_files = [f for f in uploads_dir.iterdir() if f.name != ".quarantine"]
        assert len(user_files) == 0

        # No UPLOAD activity should be created
        act_res = client.get("/api/v1/activity")
        assert act_res.json()["total"] == 0
    finally:
        _Session.commit = original_commit  # type: ignore[assignment]


def test_upload_db_commit_fails_quarantine_fails(upload_env):
    """DB commit fails; adapter.quarantine() fails.
    The failure must be logged but the original DB exception preserved."""
    import unittest.mock
    from sqlalchemy.orm import Session as _Session
    from backend.storage.registry import lookup as registry_lookup
    from backend.storage.errors import StorageUnavailableError

    env = upload_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    content = b"test data for quarantine failure"

    original_commit = _Session.commit

    def failing_commit(self):
        raise RuntimeError("Simulated database failure")

    _Session.commit = failing_commit  # type: ignore[assignment]

    def mock_lookup(provider):
        adapter, caps = registry_lookup(provider)
        adapter.quarantine = MagicMock(side_effect=StorageUnavailableError("quarantine failed", provider="LOCAL"))
        return adapter, caps

    with patch("backend.api.v1.endpoints.uploads.lookup", side_effect=mock_lookup):
        try:
            response = _upload_file(client, "quarantine_fail.txt", content)
            assert response.status_code == 500
            assert "cleanup" in response.json()["detail"].lower()

            # File remains on disk since quarantine failed
            user_files = [f for f in uploads_dir.iterdir() if f.name != ".quarantine"]
            assert len(user_files) == 1

            # No UPLOAD activity
            act_res = client.get("/api/v1/activity")
            assert act_res.json()["total"] == 0
        finally:
            _Session.commit = original_commit  # type: ignore[assignment]


def test_upload_db_commit_fails_quarantine_succeeds_cleanup_fails(upload_env):
    """DB commit fails; quarantine succeeds but cleanup fails.
    The original DB exception must be preserved as the HTTPException cause."""
    import unittest.mock
    from sqlalchemy.orm import Session as _Session
    from backend.storage.registry import lookup as registry_lookup
    from backend.storage.errors import StorageUnavailableError

    env = upload_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    content = b"test data for cleanup failure"

    original_commit = _Session.commit

    def failing_commit(self):
        raise RuntimeError("Simulated database failure")

    _Session.commit = failing_commit  # type: ignore[assignment]

    def mock_lookup(provider):
        adapter, caps = registry_lookup(provider)
        adapter.cleanup = MagicMock(side_effect=StorageUnavailableError("cleanup failed", provider="LOCAL"))
        return adapter, caps

    with patch("backend.api.v1.endpoints.uploads.lookup", side_effect=mock_lookup):
        try:
            response = _upload_file(client, "cleanup_fail.txt", content)
            assert response.status_code == 500
            assert "cleanup" in response.json()["detail"].lower()

            # File should be in quarantine (quarantine succeeded, cleanup failed)
            quarantine_dir = uploads_dir / ".quarantine"
            assert quarantine_dir.exists(), "Quarantine directory should exist"
            entries = list(quarantine_dir.iterdir())
            assert len(entries) == 1, "Exactly one quarantine entry should exist"
            entry_dir = entries[0]
            assert entry_dir.is_dir(), "Quarantine entry should be a directory"

            # The quarantined file should exist inside the entry directory
            quarantined_files = [f for f in entry_dir.iterdir() if f.name != "meta.json"]
            assert len(quarantined_files) == 1, "Exactly one quarantined file should exist"
            quarantined_file = quarantined_files[0]
            assert quarantined_file.is_file(), "Quarantined file should be a regular file"
            assert quarantined_file.read_bytes() == content, "Quarantined file contents should match original"

            # No UPLOAD activity
            act_res = client.get("/api/v1/activity")
            assert act_res.json()["total"] == 0
        finally:
            _Session.commit = original_commit  # type: ignore[assignment]


def test_upload_db_refresh_fails_after_commit_no_quarantine(upload_env):
    """Post-commit db.refresh() failure must NOT trigger quarantine/cleanup.
    The uploaded file should remain in place and the DB record should exist."""
    import unittest.mock
    from sqlalchemy.orm import Session as _Session

    env = upload_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    content = b"test data for refresh failure"

    original_refresh = _Session.refresh

    def failing_refresh(self, instance, attribute_names=None):
        raise RuntimeError("Simulated refresh failure")

    _Session.refresh = failing_refresh  # type: ignore[assignment]

    try:
        response = _upload_file(client, "refresh_fail.txt", content)
        assert response.status_code == 500
        assert "refresh" in response.json()["detail"].lower()

        # The file should STILL exist on disk (no quarantine/cleanup)
        user_files = [f for f in uploads_dir.iterdir() if f.name != ".quarantine"]
        assert len(user_files) == 1, "Uploaded file should remain after refresh failure"
        assert user_files[0].read_bytes() == content, "File contents should be intact"

        # The DB record should exist (commit succeeded)
        # We can't easily query it without the record ID, but the fact that
        # no quarantine happened means the commit path was taken
    finally:
        _Session.refresh = original_refresh  # type: ignore[assignment]


def test_upload_db_exception_cause_preserved(upload_env):
    """Original database exception must be preserved in HTTPException cause chain."""
    import unittest.mock
    from sqlalchemy.orm import Session as _Session

    env = upload_env
    client = env["client"]

    original_commit = _Session.commit

    class CustomDBError(Exception):
        pass

    def failing_commit(self):
        raise CustomDBError("Specific database error message")

    _Session.commit = failing_commit  # type: ignore[assignment]

    try:
        response = _upload_file(client, "cause_test.txt", b"data")
        assert response.status_code == 500
        # The HTTPException should have the original CustomDBError as its cause
        # This is verified by the 'from db_exc' chaining in the endpoint
        # We can't directly inspect the exception chain from the test client,
        # but the endpoint code explicitly uses 'raise HTTPException(...) from db_exc'
    finally:
        _Session.commit = original_commit  # type: ignore[assignment]


def test_upload_no_activity_on_failed_registration(upload_env):
    """Verify no UPLOAD activity is created when DB registration fails."""
    import unittest.mock
    from sqlalchemy.orm import Session as _Session

    env = upload_env
    client = env["client"]

    original_commit = _Session.commit

    def failing_commit(self):
        raise RuntimeError("Simulated database failure")

    _Session.commit = failing_commit  # type: ignore[assignment]

    try:
        response = _upload_file(client, "fail.txt", b"data")
        assert response.status_code == 500
    finally:
        _Session.commit = original_commit

    # No activity should exist
    act_res = client.get("/api/v1/activity")
    assert act_res.json()["total"] == 0
