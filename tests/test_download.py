"""Tests for the file download endpoint ``GET /api/v1/files/{file_id}/download``.

Uses isolated temporary upload directories and in-memory SQLite databases so
the development database and real uploads directory are never touched.
"""

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
def download_env(tmp_path, monkeypatch):
    """Yields an isolated test environment with a temp uploads dir and in-memory DB.

    Returns a dict with:
    - client: TestClient wired to the in-memory DB
    - engine: the SQLAlchemy engine
    - uploads_dir: Path to the temporary uploads directory
    """
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
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
    try:
        yield {
            "client": TestClient(app),
            "engine": engine,
            "uploads_dir": uploads_dir,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _upload(client, filename: str, content: bytes) -> dict:
    """Upload a file via the real upload endpoint and return the JSON payload."""
    files = {"file": (filename, content, "application/octet-stream")}
    response = client.post("/api/v1/uploads", files=files)
    assert response.status_code == 200
    return response.json()


def _seed_local_record(engine, provider_file_id: str, name: str, mime_type: str = "text/plain"):
    """Insert a LOCAL UnifiedFileRecord directly into the database."""
    session = sessionmaker(bind=engine)()
    try:
        record = UnifiedFileRecord(
            provider="LOCAL",
            provider_account_id=None,
            provider_file_id=provider_file_id,
            name=name,
            logical_path=f"/{name}",
            mime_type=mime_type,
            size_bytes=10,
            sync_state="LOCAL",
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record
    finally:
        session.close()


def test_download_real_uploaded_file(download_env):
    """A real uploaded file must be downloadable with its exact bytes."""
    env = download_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    content = b"download me, SiloVault!"
    payload = _upload(client, "report.txt", content)
    file_id = payload["id"]

    response = client.get(f"/api/v1/files/{file_id}/download")
    assert response.status_code == 200
    assert response.content == content

    # The physical file must still exist and be untouched (no deletion).
    stored = uploads_dir / payload["provider_file_id"]
    assert stored.exists()
    assert stored.read_bytes() == content


def test_download_filename_is_original_name(download_env):
    """The Content-Disposition header must carry the original filename.

    Starlette's FileResponse uses RFC 5987 encoding (filename*=utf-8''...).
    """
    env = download_env
    client = env["client"]

    payload = _upload(client, "my original name.txt", b"abc")
    response = client.get(f"/api/v1/files/{payload['id']}/download")

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    # RFC 5987 encoded: filename*=utf-8''my%20original%20name.txt
    assert "filename*=utf-8''my%20original%20name.txt" in disposition


def test_download_mime_type_is_stored_mime(download_env):
    """The response Content-Type must match the stored MIME type."""
    env = download_env
    client = env["client"]

    payload = _upload(client, "doc.txt", b"hello")
    response = client.get(f"/api/v1/files/{payload['id']}/download")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


def test_download_missing_record_returns_404(download_env):
    """A non-existent internal file ID must return HTTP 404."""
    client = download_env["client"]
    response = client.get("/api/v1/files/file_does_not_exist_12345/download")
    assert response.status_code == 404
    assert "detail" in response.json()


def test_download_missing_physical_file_returns_404(download_env):
    """A LOCAL record whose physical file is gone must return HTTP 404."""
    env = download_env
    client = env["client"]
    engine = env["engine"]
    uploads_dir = env["uploads_dir"]

    # Seed a record pointing at a storage filename that was never written.
    record = _seed_local_record(engine, "ghost_uuid_no_file.txt", "ghost.txt")

    response = client.get(f"/api/v1/files/{record.id}/download")
    assert response.status_code == 404

    # The uploads directory must remain empty (no file was created).
    assert list(uploads_dir.iterdir()) == []


def test_download_non_local_provider_rejected(download_env):
    """Cloud-provider records must not be downloadable from local storage."""
    env = download_env
    client = env["client"]
    engine = env["engine"]

    session = sessionmaker(bind=engine)()
    try:
        record = UnifiedFileRecord(
            provider="GOOGLE_DRIVE",
            provider_account_id="acc_1",
            provider_file_id="gdrive_123",
            name="cloud.pdf",
            logical_path="/cloud.pdf",
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
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "LOCAL" in detail


def test_download_traversal_filename_cannot_escape_uploads_dir(download_env):
    """A crafted provider_file_id with '..' must not escape the uploads dir."""
    env = download_env
    client = env["client"]
    engine = env["engine"]
    uploads_dir = env["uploads_dir"]
    tmp_root = uploads_dir.parent

    # Place a decoy file OUTSIDE the uploads directory.
    decoy = tmp_root / "secret.txt"
    decoy.write_text("top secret")

    record = _seed_local_record(engine, "../secret.txt", "traversal.txt")

    response = client.get(f"/api/v1/files/{record.id}/download")
    assert response.status_code == 400

    # The decoy file must remain untouched and was never served.
    assert decoy.read_text() == "top secret"


def test_download_absolute_path_filename_rejected(download_env):
    """A provider_file_id that is an absolute path must be rejected."""
    env = download_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_local_record(engine, "/etc/passwd", "abs.txt")

    response = client.get(f"/api/v1/files/{record.id}/download")
    assert response.status_code == 400


def test_download_does_not_affect_existing_endpoints(download_env):
    """Upload, list, and detail endpoints must behave exactly as before."""
    env = download_env
    client = env["client"]

    # Upload still works
    payload = _upload(client, "keep.txt", b"still here")
    assert payload["name"] == "keep.txt"
    assert payload["provider"] == "LOCAL"

    # List still works and shows the record
    list_response = client.get("/api/v1/files")
    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["total"] == 1
    assert list_payload["items"][0]["id"] == payload["id"]

    # Detail still works
    get_response = client.get(f"/api/v1/files/{payload['id']}")
    assert get_response.status_code == 200
    assert get_response.json()["name"] == "keep.txt"

    # Download still works after all of the above
    download_response = client.get(f"/api/v1/files/{payload['id']}/download")
    assert download_response.status_code == 200
    assert download_response.content == b"still here"
