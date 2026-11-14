"""Tests for Google Drive file download via the API endpoint.

These tests verify that the ``GET /api/v1/files/{file_id}/download`` endpoint
supports ``GOOGLE_DRIVE`` provider records by dispatching to the registered
adapter, while preserving the existing ``LOCAL`` download behaviour.
"""
from __future__ import annotations

import json
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import settings
from backend.database import Base, get_db
from backend.main import app
from backend.models.unified_file import UnifiedFileRecord
from backend.storage.capabilities import CAPABILITY_MAP
from backend.storage.errors import (
    StorageAuthenticationError,
    StorageNotFoundError,
    StoragePermissionError,
    StorageUnavailableError,
)
from backend.storage.google_drive import GoogleDriveAdapter
from backend.storage.registry import register, remove


# ---------------------------------------------------------------------------
# Helper: a minimal mock Google Drive adapter whose ``download`` returns
# predetermined bytes without touching the real Drive API.
# ---------------------------------------------------------------------------

class _MockGDriveAdapter(GoogleDriveAdapter):
    """Mock Google Drive adapter for testing."""

    def __init__(self, download_bytes: bytes = b"mocked content"):
        super().__init__()
        self._download_bytes = download_bytes
        # Pretend we are already authenticated so ``download`` proceeds.
        self._drive_service = object()

    def download(self, provider_file_id: str) -> bytes:  # type: ignore[override]
        """Return mock bytes, raising NotFound for unknown IDs."""
        if provider_file_id == "nonexistent_file_id":
            raise StorageNotFoundError(
                provider_file_id or "", provider="GOOGLE_DRIVE"
            )
        return self._download_bytes


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_keyring(monkeypatch):
    """Replace keyring functions so tests don't hit the OS keyring backend."""
    import backend.storage.credentials as kr_mod

    def mock_store(creds):  # noqa: D401
        pass

    def mock_load():  # noqa: D401
        return None

    def mock_delete():  # noqa: D401
        return False

    def mock_has(service, account):  # noqa: D401
        return False

    monkeypatch.setattr(kr_mod, "store_google_drive_credentials", mock_store)
    monkeypatch.setattr(kr_mod, "load_google_drive_credentials", mock_load)
    monkeypatch.setattr(kr_mod, "delete_google_drive_credentials", mock_delete)
    monkeypatch.setattr(kr_mod, "has_google_drive_credentials", mock_has)


@pytest.fixture
def gdrive_env(tmp_path, monkeypatch):
    """Isolated test environment with in-memory DB + mock GOOGLE_DRIVE adapter.

    Yields a dict with:
    - client:  TestClient wired to the in-memory DB
    - engine:  the SQLAlchemy engine
    - uploads_dir: Path to the temporary uploads directory
    """
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()

    # Override uploads dir so the LOCAL adapter (still registered) uses our temp dir
    monkeypatch.setattr(settings, "uploads_dir", str(uploads_dir))

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

    # Register LOCAL adapter (keeps existing behaviour intact)
    from backend.storage.local import LocalAdapter
    local_adapter = LocalAdapter(str(uploads_dir))
    register("LOCAL", local_adapter, CAPABILITY_MAP["LOCAL"])

    # Register a mock Google Drive adapter
    gdrive_mock = _MockGDriveAdapter(download_bytes=b"gdrive-mock-content")
    register("GOOGLE_DRIVE", gdrive_mock, CAPABILITY_MAP["GOOGLE_DRIVE"])

    try:
        yield {
            "client": TestClient(app),
            "engine": engine,
            "uploads_dir": uploads_dir,
        }
    finally:
        try:
            remove("LOCAL")
        except StorageUnavailableError:
            pass  # already unregistered from a previous test run
        try:
            remove("GOOGLE_DRIVE")
        except StorageUnavailableError:
            pass  # already unregistered from a previous test run
        app.dependency_overrides.clear()
        engine.dispose()


# ---------------------------------------------------------------------------
# Helper: upload a file via the real upload endpoint (LOCAL only) and return
# the JSON payload.  Used when we need a LOCAL record in the same test.
# ---------------------------------------------------------------------------

def _upload_local(client, filename: str, content: bytes) -> dict:
    """Upload via the real endpoint and return the JSON payload."""
    files = {"file": (filename, content, "application/octet-stream")}
    response = client.post("/api/v1/uploads", files=files)
    assert response.status_code == 200
    return response.json()


# ---------------------------------------------------------------------------
# Tests: Google Drive download
# ---------------------------------------------------------------------------


def test_google_drive_download_success(gdrive_env):
    """A Google Drive file must be downloadable with its exact bytes."""
    env = gdrive_env
    client = env["client"]
    engine = env["engine"]

    # Insert a GOOGLE_DRIVE record directly (no physical file needed).
    session = sessionmaker(bind=engine)()
    try:
        record = UnifiedFileRecord(
            provider="GOOGLE_DRIVE",
            provider_account_id=None,
            provider_file_id="gdrive_abc123",
            name="report.pdf",
            logical_path="/report.pdf",
            mime_type="application/pdf",
            size_bytes=2048,
            sync_state="COMPLETED",
        )
        session.add(record)
        session.commit()
        session.refresh(record)
    finally:
        session.close()

    # The mock adapter's download returns predetermined bytes.
    # No need to monkeypatch — the registered adapter handles it.

    response = client.get(f"/api/v1/files/{record.id}/download")
    assert response.status_code == 200
    assert response.content == b"gdrive-mock-content"

    # Verify MIME type.
    assert response.headers["content-type"].startswith("application/pdf")

    # Verify Content-Disposition header.
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    assert "filename*=utf-8''report.pdf" in disposition


def test_google_drive_download_missing_record(gdrive_env):
    """A non-existent internal file ID must return HTTP 404."""
    env = gdrive_env
    client = env["client"]

    response = client.get("/api/v1/files/file_does_not_exist_12345/download")
    assert response.status_code == 404
    assert "detail" in response.json()


def test_google_drive_download_authentication_failure(gdrive_env):
    """Google Drive authentication failure must map to HTTP 401."""
    env = gdrive_env
    client = env["client"]

    # Monkeypatch the registered adapter so that ``download`` raises
    # ``StorageAuthenticationError``.
    from backend.storage.errors import StorageAuthenticationError

    # Instead, patch the adapter that was registered under "GOOGLE_DRIVE".
    from backend.storage.registry import lookup as _lookup
    adapter_obj, _ = _lookup("GOOGLE_DRIVE")

    original_download = adapter_obj.download

    def failing_download(provider_file_id):  # noqa: D401
        raise StorageAuthenticationError(
            detail="Invalid refresh token.", provider="GOOGLE_DRIVE"
        )

    adapter_obj.download = failing_download  # type: ignore[method-assign]

    # Insert a record so the endpoint reaches the download step.
    session = sessionmaker(bind=env["engine"])()
    try:
        record = UnifiedFileRecord(
            provider="GOOGLE_DRIVE",
            provider_account_id=None,
            provider_file_id="gdrive_abc123",
            name="report.pdf",
            logical_path="/report.pdf",
            mime_type="application/pdf",
            size_bytes=2048,
            sync_state="COMPLETED",
        )
        session.add(record)
        session.commit()
        session.refresh(record)
    finally:
        session.close()

    response = client.get(f"/api/v1/files/{record.id}/download")
    assert response.status_code == 401

    # restore original
    adapter_obj.download = original_download


def test_google_drive_download_file_not_found(gdrive_env):
    """A Google Drive file ID that does not exist on the provider must return 404."""
    env = gdrive_env
    client = env["client"]
    engine = env["engine"]

    session = sessionmaker(bind=engine)()
    try:
        record = UnifiedFileRecord(
            provider="GOOGLE_DRIVE",
            provider_account_id=None,
            provider_file_id="nonexistent_file_id",
            name="missing.pdf",
            logical_path="/missing.pdf",
            mime_type="application/pdf",
            size_bytes=512,
            sync_state="COMPLETED",
        )
        session.add(record)
        session.commit()
        session.refresh(record)
    finally:
        session.close()

    # The mock adapter's ``download`` will raise ``StorageNotFoundError``
    # because the provider_file_id does not match any real Drive file.
    # (The adapter does not validate against a real API in this test.)
    response = client.get(f"/api/v1/files/{record.id}/download")
    assert response.status_code == 404


def test_google_drive_download_provider_unavailable(gdrive_env):
    """Provider unavailable (e.g. registry not registered) must return 503."""
    env = gdrive_env
    client = env["client"]

    # Remove the GOOGLE_DRIVE adapter so ``lookup`` raises ``StorageUnavailableError``.
    # Use try/except so the fixture's finally block can also call remove safely.
    try:
        remove("GOOGLE_DRIVE")
    except StorageUnavailableError:
        pass  # already unregistered

    # Insert a GOOGLE_DRIVE record so the endpoint reaches the lookup step.
    session = sessionmaker(bind=env["engine"])()
    try:
        record = UnifiedFileRecord(
            provider="GOOGLE_DRIVE",
            provider_account_id=None,
            provider_file_id="gdrive_abc123",
            name="report.pdf",
            logical_path="/report.pdf",
            mime_type="application/pdf",
            size_bytes=2048,
            sync_state="COMPLETED",
        )
        session.add(record)
        session.commit()
        session.refresh(record)
    finally:
        session.close()

    response = client.get(f"/api/v1/files/{record.id}/download")
    # lookup() raises StorageUnavailableError → 503.
    assert response.status_code == 503


def test_local_download_regression(gdrive_env):
    """LOCAL download must still work exactly as before despite the new GOOGLE_DRIVE path."""
    env = gdrive_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    # Upload a file via the real endpoint (LOCAL).
    def _upload_local(c, fn, ct):
        files = {"file": (fn, ct, "application/octet-stream")}
        r = c.post("/api/v1/uploads", files=files)
        assert r.status_code == 200
        return r.json()

    payload = _upload_local(client, "regression.txt", b"still works")
    file_id = payload["id"]

    # Download must succeed and return the original bytes.
    response = client.get(f"/api/v1/files/{file_id}/download")
    assert response.status_code == 200
    assert response.content == b"still works"

    # The physical file must be untouched on disk.
    stored = uploads_dir / payload["provider_file_id"]
    assert stored.exists()
    assert stored.read_bytes() == b"still works"


def test_local_download_with_mime_and_headers(gdrive_env):
    """LOCAL download must return correct MIME type and Content-Disposition headers."""
    env = gdrive_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    def _upload_local(c, fn, ct):
        files = {"file": (fn, ct, "application/octet-stream")}
        r = c.post("/api/v1/uploads", files=files)
        assert r.status_code == 200
        return r.json()

    payload = _upload_local(client, "doc.txt", b"hello world")
    response = client.get(f"/api/v1/files/{payload['id']}/download")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    assert "filename*=utf-8''doc.txt" in disposition