"""Tests for Google Drive download (M3.2 Phase B1.3).

All tests use a mocked keyring (same pattern as ``test_google_drive_root_folder.py``)
and a mocked Drive API service — no real Google Drive API calls are made and
no OS credential store is required.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from backend.storage.credentials import GoogleDriveCredentials, store_google_drive_credentials
from backend.storage.errors import (
    StorageAuthenticationError,
    StorageNotFoundError,
    StoragePermissionError,
    StorageQuotaExceededError,
    StorageRateLimitError,
    StorageUnavailableError,
)
from backend.storage.google_drive import GoogleDriveAdapter

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


def _seed_credentials() -> None:
    """Store valid credentials in the fake keyring."""
    store_google_drive_credentials(
        GoogleDriveCredentials(
            refresh_token="rt-123",
            client_id="cid",
            client_secret="cs",
            scopes=[_DRIVE_FILE_SCOPE],
        )
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


# ---------------------------------------------------------------------------
# Tests: successful download
# ---------------------------------------------------------------------------


def test_successful_download(_fake_keyring):
    """Download returns the exact bytes provided by MediaIoBaseDownload."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value

    content = b"exact plaintext bytes for download"

    with patch("googleapiclient.http.MediaIoBaseDownload") as mock_downloader_cls:
        downloader_instance = MagicMock()
        mock_downloader_cls.return_value = downloader_instance

        # Simulate single-chunk completion
        def mock_next_chunk():
            stream = mock_downloader_cls.call_args.args[0]
            stream.write(content)
            return (MagicMock(progress=1.0), True)

        downloader_instance.next_chunk.side_effect = mock_next_chunk

        result = adapter.download("drive-file-123")

    assert result == content
    files_mock.get_media.assert_called_once_with(fileId="drive-file-123")


def test_multi_chunk_download(_fake_keyring):
    """Download accumulates bytes across multiple chunks until done is True."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value

    chunk1 = b"chunk one "
    chunk2 = b"chunk two "
    chunk3 = b"chunk three"

    with patch("googleapiclient.http.MediaIoBaseDownload") as mock_downloader_cls:
        downloader_instance = MagicMock()
        mock_downloader_cls.return_value = downloader_instance

        chunks = [(chunk1, False), (chunk2, False), (chunk3, True)]
        chunk_iter = iter(chunks)

        def mock_next_chunk():
            data, is_done = next(chunk_iter)
            stream = mock_downloader_cls.call_args.args[0]
            stream.write(data)
            return (MagicMock(), is_done)

        downloader_instance.next_chunk.side_effect = mock_next_chunk

        result = adapter.download("large-file-456")

    assert result == chunk1 + chunk2 + chunk3
    assert downloader_instance.next_chunk.call_count == 3


def test_empty_file_download(_fake_keyring):
    """Downloading a 0-byte file returns empty bytes."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value

    with patch("googleapiclient.http.MediaIoBaseDownload") as mock_downloader_cls:
        downloader_instance = MagicMock()
        mock_downloader_cls.return_value = downloader_instance
        downloader_instance.next_chunk.return_value = (MagicMock(progress=1.0), True)

        result = adapter.download("empty-file-000")

    assert result == b""
    files_mock.get_media.assert_called_once_with(fileId="empty-file-000")


# ---------------------------------------------------------------------------
# Tests: input validation
# ---------------------------------------------------------------------------


def test_download_unauthenticated_raises_authentication_error():
    """Download without an active Drive service raises StorageAuthenticationError."""
    adapter = GoogleDriveAdapter()

    with pytest.raises(StorageAuthenticationError) as excinfo:
        adapter.download("file-123")

    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_download_empty_or_whitespace_file_id_raises_not_found(_fake_keyring):
    """Empty or whitespace-only provider_file_id is rejected with StorageNotFoundError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()

    for invalid_id in ("", "   ", "\t"):
        with pytest.raises(StorageNotFoundError) as excinfo:
            adapter.download(invalid_id)
        assert excinfo.value.provider == "GOOGLE_DRIVE"


# ---------------------------------------------------------------------------
# Tests: Google API error mapping
# ---------------------------------------------------------------------------


def test_download_http_404_maps_to_not_found_error(_fake_keyring):
    """HttpError 404 maps to StorageNotFoundError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.get_media.side_effect = _make_http_error(404, "File not found")

    with pytest.raises(StorageNotFoundError) as excinfo:
        adapter.download("nonexistent-file")

    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_download_http_403_maps_to_permission_error(_fake_keyring):
    """HttpError 403 maps to StoragePermissionError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.get_media.side_effect = _make_http_error(
        403, "Forbidden", error_reason="forbidden"
    )

    with pytest.raises(StoragePermissionError) as excinfo:
        adapter.download("forbidden-file")

    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_download_http_429_maps_to_rate_limit_error(_fake_keyring):
    """HttpError 429 maps to StorageRateLimitError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.get_media.side_effect = _make_http_error(429, "Too Many Requests")

    with pytest.raises(StorageRateLimitError) as excinfo:
        adapter.download("throttled-file")

    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_download_http_403_rate_limit_reason_maps_to_rate_limit_error(_fake_keyring):
    """HttpError 403 with userRateLimitExceeded maps to StorageRateLimitError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.get_media.side_effect = _make_http_error(
        403, "Forbidden", error_reason="userRateLimitExceeded"
    )

    with pytest.raises(StorageRateLimitError) as excinfo:
        adapter.download("throttled-file")

    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_download_http_413_maps_to_quota_exceeded_error(_fake_keyring):
    """HttpError 413 maps to StorageQuotaExceededError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.get_media.side_effect = _make_http_error(
        413, "Payload Too Large", error_reason="fileTooLarge"
    )

    with pytest.raises(StorageQuotaExceededError) as excinfo:
        adapter.download("huge-file")

    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_download_http_500_maps_to_unavailable_error(_fake_keyring):
    """HttpError 500 maps to StorageUnavailableError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.get_media.side_effect = _make_http_error(500, "Internal Server Error")

    with pytest.raises(StorageUnavailableError) as excinfo:
        adapter.download("server-error-file")

    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_download_next_chunk_error_mapped(_fake_keyring):
    """An HttpError raised during chunk downloading is mapped properly."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()

    with patch("googleapiclient.http.MediaIoBaseDownload") as mock_downloader_cls:
        downloader_instance = MagicMock()
        mock_downloader_cls.return_value = downloader_instance
        downloader_instance.next_chunk.side_effect = _make_http_error(503, "Service Unavailable")

        with pytest.raises(StorageUnavailableError) as excinfo:
            adapter.download("interrupted-file")

        assert excinfo.value.provider == "GOOGLE_DRIVE"
