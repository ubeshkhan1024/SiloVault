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
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import settings
from backend.database import Base, get_db
from backend.main import app
from backend.models.unified_file import UnifiedFileRecord


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
    try:
        yield {
            "client": TestClient(app),
            "engine": engine,
            "uploads_dir": uploads_dir,
        }
    finally:
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
        assert "cleaned up" in response.json()["detail"].lower()
    finally:
        _Session.commit = original_session_commit  # type: ignore[assignment]

    # The file should have been cleaned up
    assert len(list(uploads_dir.iterdir())) == 0


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
