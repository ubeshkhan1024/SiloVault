"""Tests for Google Drive upload (M3.2 Phase B1.2).

All tests use a mocked keyring (same pattern as ``test_google_drive_root_folder.py``)
and a mocked Drive API service — no real Google Drive API calls are made and
no OS credential store is required.

Endpoint-level tests use an in-memory SQLite database and a mocked
``GOOGLE_DRIVE`` adapter registered in the provider registry, so the
development database and real uploads directory are never touched.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import settings
from backend.database import Base, get_db
from backend.main import app
from backend.models.unified_file import UnifiedFileRecord
from backend.storage.capabilities import CAPABILITY_MAP
from backend.storage.credentials import GoogleDriveCredentials, store_google_drive_credentials
from backend.storage.errors import (
    StorageAuthenticationError,
    StorageInvalidResponseError,
    StorageNotFoundError,
    StoragePermissionError,
    StorageQuotaExceededError,
    StorageRateLimitError,
    StorageUnavailableError,
)
from backend.storage.google_drive import GoogleDriveAdapter
from backend.storage.local import LocalAdapter
from backend.storage.registry import register, remove

_KEYRING_SERVICE = "silovault"
_KEYRING_ACCOUNT = "google-drive"
_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fake_keyring(monkeypatch: pytest.MonkeyPatch):
    """Replace ``_get_keyring`` with one that returns an in-memory dict."""

    class InMemoryKeyring:
        def __init__(self):
            self.store: dict[tuple[str, str], str] = {}

        def set_password(self, service: str, account: str, data: str) -> None:
            self.store[(service, account)] = data

        def get_password(self, service: str, account: str) -> str | None:
            return self.store.get((service, account))

        def delete_password(self, service: str, account: str) -> None:
            self.store.pop((service, account), None)

    fake_kr = InMemoryKeyring()

    def fake_get_keyring():
        return fake_kr

    monkeypatch.setattr("backend.storage.credentials._get_keyring", fake_get_keyring)
    monkeypatch.setattr("backend.storage.credentials.keyring", SimpleNamespace())

    yield fake_kr


def _seed_credentials(root_folder_id: str = "root-folder-123") -> None:
    """Store valid credentials (with a root folder ID) in the fake keyring."""
    store_google_drive_credentials(
        GoogleDriveCredentials(
            refresh_token="rt-123",
            client_id="cid",
            client_secret="cs",
            scopes=[_DRIVE_FILE_SCOPE],
        ).with_root_folder_id(root_folder_id)
    )


def _make_authenticated_adapter() -> GoogleDriveAdapter:
    """Build an adapter with a mocked Drive service (bypasses real OAuth)."""
    adapter = GoogleDriveAdapter()
    adapter._drive_service = MagicMock()
    return adapter


def _make_http_error(
    status: int,
    reason: str = "Error",
    error_reason: str | None = None,
) -> HttpError:
    """Construct a realistic googleapiclient HttpError for a given status."""
    resp = MagicMock()
    resp.status = status
    resp.reason = reason
    errors = [{"reason": error_reason, "domain": "global"}] if error_reason else []
    body = json.dumps({"error": {"message": reason, "errors": errors}}).encode("utf-8")
    return HttpError(resp, body)


def _upload_response(
    file_id: str = "file-abc-123",
    **overrides: object,
) -> dict:
    """Build a typical Drive API upload response."""
    response = {
        "id": file_id,
        "name": "report.pdf",
        "mimeType": "application/pdf",
        "size": "1024",
        "modifiedTime": "2026-11-14T01:00:00Z",
        "md5Checksum": "d41d8cd98f00b204e9800998ecf8427e",
    }
    response.update(overrides)
    return response


# ---------------------------------------------------------------------------
# Tests: adapter-level upload
# ---------------------------------------------------------------------------


def test_successful_google_drive_upload(_fake_keyring):
    """A successful upload returns a ProviderFileRecord with the Drive file ID."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.create.return_value.execute.return_value = _upload_response()

    content = b"pdf-bytes" * 100
    record = adapter.upload(content, "report.pdf", "/report.pdf")

    assert record.provider_file_id == "file-abc-123"
    assert record.name == "report.pdf"
    assert record.logical_path == "/report.pdf"
    assert record.mime_type == "application/pdf"
    files_mock.create.assert_called_once()


def test_root_folder_id_used_as_parent(_fake_keyring):
    """The SiloVault root folder ID from the credential record is used as
    the file's parent, and the body carries name and MIME type."""
    _seed_credentials(root_folder_id="root-folder-123")
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.create.return_value.execute.return_value = _upload_response()

    adapter.upload(b"data", "report.pdf", "/report.pdf")

    call_kwargs = files_mock.create.call_args.kwargs
    assert call_kwargs["body"]["parents"] == ["root-folder-123"]
    assert call_kwargs["body"]["name"] == "report.pdf"
    assert call_kwargs["body"]["mimeType"] == "application/pdf"


def test_resumable_upload_configured(_fake_keyring):
    """MediaIoBaseUpload is constructed with resumable=True and the detected
    MIME type, and the resulting media object is passed to files().create()."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.create.return_value.execute.return_value = _upload_response(
        name="note.txt", mimeType="text/plain", size="5"
    )

    with patch("googleapiclient.http.MediaIoBaseUpload") as mock_upload_cls:
        mock_media = MagicMock()
        mock_upload_cls.return_value = mock_media
        adapter.upload(b"hello", "note.txt", "/note.txt")

    mock_upload_cls.assert_called_once()
    _, media_kwargs = mock_upload_cls.call_args
    assert media_kwargs.get("resumable") is True
    assert media_kwargs.get("mimetype") == "text/plain"
    # The file bytes are handed to the media body as a seekable stream
    stream = mock_upload_cls.call_args.args[0]
    assert stream.read() == b"hello"

    create_kwargs = files_mock.create.call_args.kwargs
    assert create_kwargs["media_body"] is mock_media


def test_provider_file_record_fields(_fake_keyring):
    """The returned record carries size, provider metadata, and a UTC-aware
    modified_at timestamp from the Drive response."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.create.return_value.execute.return_value = _upload_response(
        file_id="f-42", size="2048", modifiedTime="2026-11-14T01:00:00Z"
    )

    record = adapter.upload(b"x" * 10, "report.pdf", "/report.pdf")

    assert record.size_bytes == 2048
    assert record.provider_metadata["md5_checksum"] == "d41d8cd98f00b204e9800998ecf8427e"
    assert record.provider_metadata["web_view_link"] == (
        "https://drive.google.com/file/d/f-42/view"
    )
    assert record.provider_metadata["web_content_link"] == (
        "https://drive.google.com/uc?id=f-42&export=download"
    )
    assert record.modified_at is not None
    assert record.modified_at.year == 2026
    assert record.modified_at.tzinfo is not None
    # SHA-256 is computed by the endpoint, not the adapter
    assert record.sha256 is None


def test_size_falls_back_to_local_length_when_absent(_fake_keyring):
    """When the Drive response omits 'size', the local byte length is used."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.create.return_value.execute.return_value = _upload_response(
        file_id="f-42", size=None
    )
    # Remove the key entirely to simulate an absent field
    del files_mock.create.return_value.execute.return_value["size"]

    content = b"y" * 37
    record = adapter.upload(content, "data.bin", "/data.bin")

    assert record.size_bytes == 37


def test_modified_time_absent_yields_none(_fake_keyring):
    """A response without modifiedTime yields modified_at=None."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    response = _upload_response(file_id="f-42")
    del response["modifiedTime"]
    files_mock.create.return_value.execute.return_value = response

    record = adapter.upload(b"z", "data.bin", "/data.bin")

    assert record.modified_at is None


def test_mime_type_detection_and_fallback(_fake_keyring):
    """Known extensions yield their MIME type; unknown extensions fall back
    to application/octet-stream in both the media body and the request body."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.create.return_value.execute.return_value = _upload_response(
        name="mystery.xyz", mimeType="application/octet-stream", size="1"
    )

    adapter.upload(b"q", "mystery.xyz", "/mystery.xyz")

    call_kwargs = files_mock.create.call_args.kwargs
    assert call_kwargs["body"]["mimeType"] == "application/octet-stream"
    with patch("googleapiclient.http.MediaIoBaseUpload") as mock_upload_cls:
        mock_upload_cls.return_value = MagicMock()
        adapter.upload(b"q", "photo.png", "/photo.png")
    _, media_kwargs = mock_upload_cls.call_args
    assert media_kwargs.get("mimetype") == "image/png"


def test_upload_without_drive_service_raises_authentication_error(_fake_keyring):
    """An adapter that never authenticated rejects uploads safely."""
    _seed_credentials()
    adapter = GoogleDriveAdapter()

    with pytest.raises(StorageAuthenticationError) as excinfo:
        adapter.upload(b"data", "file.txt", "/file.txt")

    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_upload_rejects_non_bytes_payload(_fake_keyring):
    """Non-byes payloads are rejected before any API interaction."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()

    with pytest.raises(TypeError):
        adapter.upload("not-bytes", "file.txt", "/file.txt")  # type: ignore[arg-type]

    adapter._drive_service.files.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Google API error mapping on upload
# ---------------------------------------------------------------------------


def test_upload_http_403_maps_to_permission_error(_fake_keyring):
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.create.return_value.execute.side_effect = _make_http_error(
        403, "Forbidden", error_reason="forbidden"
    )

    with pytest.raises(StoragePermissionError) as excinfo:
        adapter.upload(b"data", "file.txt", "/file.txt")

    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_upload_http_404_maps_to_not_found_error(_fake_keyring):
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.create.return_value.execute.side_effect = _make_http_error(404, "Not Found")

    with pytest.raises(StorageNotFoundError):
        adapter.upload(b"data", "file.txt", "/file.txt")


def test_upload_http_429_maps_to_rate_limit_error(_fake_keyring):
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.create.return_value.execute.side_effect = _make_http_error(
        429, "Too Many Requests"
    )

    with pytest.raises(StorageRateLimitError):
        adapter.upload(b"data", "file.txt", "/file.txt")


def test_upload_http_403_rate_limit_reason_maps_to_rate_limit_error(_fake_keyring):
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.create.return_value.execute.side_effect = _make_http_error(
        403, "Forbidden", error_reason="userRateLimitExceeded"
    )

    with pytest.raises(StorageRateLimitError):
        adapter.upload(b"data", "file.txt", "/file.txt")


def test_upload_http_413_maps_to_quota_exceeded_error(_fake_keyring):
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.create.return_value.execute.side_effect = _make_http_error(
        413, "Request Entity Too Large", error_reason="insufficientStorage"
    )

    with pytest.raises(StorageQuotaExceededError):
        adapter.upload(b"data", "file.txt", "/file.txt")


def test_upload_http_500_maps_to_unavailable_error(_fake_keyring):
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.create.return_value.execute.side_effect = _make_http_error(
        500, "Internal Server Error"
    )

    with pytest.raises(StorageUnavailableError) as excinfo:
        adapter.upload(b"data", "file.txt", "/file.txt")

    assert excinfo.value.provider == "GOOGLE_DRIVE"


# ---------------------------------------------------------------------------
# Tests: invalid Drive responses
# ---------------------------------------------------------------------------


def test_upload_response_missing_id_is_rejected(_fake_keyring):
    """A create response without an 'id' field is a protocol violation."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.create.return_value.execute.return_value = {}

    with pytest.raises(StorageInvalidResponseError):
        adapter.upload(b"data", "file.txt", "/file.txt")


def test_upload_response_non_string_id_is_rejected(_fake_keyring):
    """A non-string 'id' is rejected as an invalid response."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.create.return_value.execute.return_value = {"id": 12345}

    with pytest.raises(StorageInvalidResponseError):
        adapter.upload(b"data", "file.txt", "/file.txt")


# ---------------------------------------------------------------------------
# Endpoint-level tests: provider selection and database registration
# ---------------------------------------------------------------------------


@pytest.fixture
def gd_upload_env(tmp_path, monkeypatch):
    """Isolated endpoint test environment: in-memory DB, temp uploads dir,
    and a mocked GOOGLE_DRIVE adapter registered in the provider registry."""
    uploads_dir = tmp_path / "uploads"
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

    # LOCAL adapter for default-provider regression coverage
    local_adapter = LocalAdapter(str(uploads_dir))

    # Real GoogleDriveAdapter instance with upload replaced by a mock;
    # kept as a genuine StorageOperations so the registry accepts it.
    gd_adapter = GoogleDriveAdapter()
    gd_adapter.get_root_folder_id = MagicMock(return_value="root-folder-123")
    gd_adapter.upload = MagicMock()

    try:
        register("LOCAL", local_adapter, CAPABILITY_MAP["LOCAL"])
    except StorageUnavailableError:
        replace("LOCAL", local_adapter, CAPABILITY_MAP["LOCAL"])

    try:
        register("GOOGLE_DRIVE", gd_adapter, CAPABILITY_MAP["GOOGLE_DRIVE"])
    except StorageUnavailableError:
        replace("GOOGLE_DRIVE", gd_adapter, CAPABILITY_MAP["GOOGLE_DRIVE"])

    try:
        yield {
            "client": TestClient(app),
            "engine": engine,
            "uploads_dir": uploads_dir,
            "local_adapter": local_adapter,
            "gd_adapter": gd_adapter,
        }
    finally:
        for provider_id in ("GOOGLE_DRIVE", "LOCAL"):
            try:
                remove(provider_id)
            except Exception:
                pass
        app.dependency_overrides.clear()
        engine.dispose()


def _count_db_records(engine) -> int:
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        return len(session.execute(select(UnifiedFileRecord)).scalars().all())


def test_endpoint_google_drive_upload_success_registers_record(gd_upload_env):
    """A successful GOOGLE_DRIVE upload registers a record with the correct
    provider, provider file ID, sync state, and client-computed SHA-256."""
    client = gd_upload_env["client"]
    engine = gd_upload_env["engine"]
    gd_adapter = gd_upload_env["gd_adapter"]

    from backend.storage.types import ProviderFileRecord

    gd_adapter.upload.return_value = ProviderFileRecord(
        provider_file_id="drive-file-777",
        name="cloud.txt",
        logical_path="/cloud.txt",
        mime_type="text/plain",
        size_bytes=11,
        sha256=None,
    )

    content = b"cloud data!"
    files = {"file": ("cloud.txt", content, "text/plain")}
    response = client.post("/api/v1/uploads", files=files, params={"provider": "GOOGLE_DRIVE"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "GOOGLE_DRIVE"
    assert payload["provider_file_id"] == "drive-file-777"
    assert payload["name"] == "cloud.txt"
    assert payload["logical_path"] == "/cloud.txt"
    assert payload["size_bytes"] == 11
    assert payload["sync_state"] == "PENDING"
    assert payload["sha256"] == hashlib.sha256(content).hexdigest()

    # The adapter received the raw bytes and original metadata
    upload_args = gd_adapter.upload.call_args.args
    assert upload_args[0] == content
    assert upload_args[1] == "cloud.txt"
    assert upload_args[2] == "/cloud.txt"

    # Exactly one record in the database
    assert _count_db_records(engine) == 1


def test_endpoint_failed_google_drive_upload_creates_no_db_record(gd_upload_env):
    """When the Drive upload fails, the endpoint returns an error and no
    database record is created."""
    client = gd_upload_env["client"]
    engine = gd_upload_env["engine"]
    gd_adapter = gd_upload_env["gd_adapter"]

    gd_adapter.upload.side_effect = StoragePermissionError(
        detail="permission denied", provider="GOOGLE_DRIVE"
    )

    files = {"file": ("denied.txt", b"data", "text/plain")}
    response = client.post(
        "/api/v1/uploads", files=files, params={"provider": "GOOGLE_DRIVE"}
    )

    assert response.status_code == 403
    assert _count_db_records(engine) == 0


def test_endpoint_google_drive_unavailable_returns_503(gd_upload_env):
    """A transient Drive failure maps to 503 and leaves no DB record."""
    client = gd_upload_env["client"]
    engine = gd_upload_env["engine"]
    gd_adapter = gd_upload_env["gd_adapter"]

    gd_adapter.upload.side_effect = StorageUnavailableError(
        detail="drive unreachable", provider="GOOGLE_DRIVE"
    )

    files = {"file": ("flaky.txt", b"data", "text/plain")}
    response = client.post(
        "/api/v1/uploads", files=files, params={"provider": "GOOGLE_DRIVE"}
    )

    assert response.status_code == 503
    assert _count_db_records(engine) == 0


def test_endpoint_default_provider_is_local(gd_upload_env):
    """Without a provider parameter, uploads still go to LOCAL with
    provider='LOCAL' and sync_state='LOCAL'."""
    client = gd_upload_env["client"]
    engine = gd_upload_env["engine"]
    uploads_dir = gd_upload_env["uploads_dir"]
    gd_adapter = gd_upload_env["gd_adapter"]

    files = {"file": ("local.txt", b"local data", "text/plain")}
    response = client.post("/api/v1/uploads", files=files)

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "LOCAL"
    assert payload["sync_state"] == "LOCAL"
    assert payload["provider_file_id"] != "local.txt"

    # File stored on local disk, Drive adapter untouched
    stored = uploads_dir / payload["provider_file_id"]
    assert stored.read_bytes() == b"local data"
    gd_adapter.upload.assert_not_called()
    assert _count_db_records(engine) == 1


def test_endpoint_invalid_provider_rejected(gd_upload_env):
    """A provider ID that violates the canonical pattern is rejected with 422
    before any adapter is consulted."""
    client = gd_upload_env["client"]
    gd_adapter = gd_upload_env["gd_adapter"]

    files = {"file": ("bad.txt", b"data", "text/plain")}
    response = client.post(
        "/api/v1/uploads", files=files, params={"provider": "google_drive"}
    )

    assert response.status_code == 422
    gd_adapter.upload.assert_not_called()
