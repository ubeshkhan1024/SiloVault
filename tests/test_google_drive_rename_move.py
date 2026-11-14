"""Tests for Google Drive rename and move (M3.2 Phase B1.4).

All tests use a mocked keyring (same pattern as ``test_google_drive_root_folder.py``)
and a mocked Drive API service — no real Google Drive API calls are made and
no OS credential store is required.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from googleapiclient.errors import HttpError

from backend.storage.credentials import GoogleDriveCredentials, store_google_drive_credentials
from backend.storage.errors import (
    StorageAuthenticationError,
    StorageInvalidResponseError,
    StorageNotFoundError,
    StoragePermissionError,
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
# Helpers: shared drive response fixtures
# ---------------------------------------------------------------------------

_SAMPLE_FILE_META = {
    "id": "file-rename-abc",
    "name": "original.pdf",
    "mimeType": "application/pdf",
    "size": "1024",
    "modifiedTime": "2024-06-15T10:00:00Z",
    "md5Checksum": "d41d8cd98f00b204e9800998ecf8427e",
}

_SAMPLE_SIZE_META = {
    "size": "1024",
    "mimeType": "application/pdf",
    "modifiedTime": "2024-06-15T10:00:00Z",
    "md5Checksum": "d41d8cd98f00b204e9800998ecf8427e",
}


# ---------------------------------------------------------------------------
# Tests: rename
# ---------------------------------------------------------------------------


def test_rename_successful(_fake_keyring):
    """A successful rename updates the file name and returns an updated record."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value

    updated_name = "renamed.pdf"
    get_response = dict(_SAMPLE_SIZE_META)
    update_response = dict(_SAMPLE_FILE_META)
    update_response["name"] = updated_name

    files_mock.get.return_value.execute.return_value = get_response
    files_mock.update.return_value.execute.return_value = update_response

    result = adapter.rename("file-rename-abc", updated_name)

    assert result.provider_file_id == "file-rename-abc"
    assert result.name == updated_name
    assert result.mime_type == "application/pdf"
    assert result.size_bytes == 1024
    assert result.logical_path == "/file-rename-abc"
    assert result.sha256 is None
    assert result.provider_metadata["md5_checksum"] == "d41d8cd98f00b204e9800998ecf8427e"
    assert "web_view_link" in result.provider_metadata
    assert "web_content_link" in result.provider_metadata

    # Verify both API calls were made in order
    files_mock.get.assert_called_once_with(
        fileId="file-rename-abc", fields="size,mimeType,modifiedTime,md5Checksum"
    )
    files_mock.update.assert_called_once_with(
        fileId="file-rename-abc",
        body={"name": updated_name},
        fields="id,name,mimeType,size,modifiedTime,md5Checksum",
    )


def test_rename_empty_file_id_raises_not_found(_fake_keyring):
    """Empty or whitespace-only provider_file_id is rejected with StorageNotFoundError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()

    for invalid_id in ("", "   ", "\t"):
        with pytest.raises(StorageNotFoundError) as excinfo:
            adapter.rename(invalid_id, "new.pdf")
        assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_rename_empty_new_name_raises_invalid_response(_fake_keyring):
    """An empty or whitespace-only new_name is rejected before any API call."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()

    for bad_name in ("", "   ", "\t"):
        with pytest.raises(StorageInvalidResponseError) as excinfo:
            adapter.rename("file-123", bad_name)
        assert excinfo.value.provider == "GOOGLE_DRIVE"

    # No Drive API call should have been made
    adapter._drive_service.files.return_value.get.assert_not_called()
    adapter._drive_service.files.return_value.update.assert_not_called()


def test_rename_unauthenticated_raises_authentication_error():
    """Rename without an active Drive service raises StorageAuthenticationError."""
    adapter = GoogleDriveAdapter()

    with pytest.raises(StorageAuthenticationError) as excinfo:
        adapter.rename("file-123", "new.pdf")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_rename_http_404_maps_to_not_found_error(_fake_keyring):
    """HttpError 404 during get() is mapped to StorageNotFoundError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.get.side_effect = _make_http_error(404, "File not found")

    with pytest.raises(StorageNotFoundError) as excinfo:
        adapter.rename("missing-file", "new.pdf")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_rename_http_403_maps_to_permission_error(_fake_keyring):
    """HttpError 403 maps to StoragePermissionError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.get.side_effect = _make_http_error(
        403, "Forbidden", error_reason="forbidden"
    )

    with pytest.raises(StoragePermissionError) as excinfo:
        adapter.rename("forbidden-file", "new.pdf")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_rename_http_429_maps_to_rate_limit_error(_fake_keyring):
    """HttpError 429 maps to StorageRateLimitError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.get.side_effect = _make_http_error(429, "Too Many Requests")

    with pytest.raises(StorageRateLimitError) as excinfo:
        adapter.rename("throttled-file", "new.pdf")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_rename_http_500_maps_to_unavailable_error(_fake_keyring):
    """HttpError 500 maps to StorageUnavailableError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.get.side_effect = _make_http_error(500, "Internal Server Error")

    with pytest.raises(StorageUnavailableError) as excinfo:
        adapter.rename("server-error-file", "new.pdf")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_rename_update_missing_id_raises_invalid_response(_fake_keyring):
    """A Drive update response missing 'id' is rejected with StorageInvalidResponseError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.get.return_value.execute.return_value = dict(_SAMPLE_SIZE_META)
    adapter._drive_service.files.return_value.update.return_value.execute.return_value = {}

    with pytest.raises(StorageInvalidResponseError) as excinfo:
        adapter.rename("file-with-no-id", "new.pdf")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


# ---------------------------------------------------------------------------
# Tests: move
# ---------------------------------------------------------------------------


def test_move_successful(_fake_keyring):
    """A successful move updates logical_path and returns an updated record."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value

    target_path = "/Archive/2024/invoice.pdf"
    files_mock.get.return_value.execute.return_value = dict(_SAMPLE_FILE_META)

    result = adapter.move("file-rename-abc", target_path)

    assert result.provider_file_id == "file-rename-abc"
    assert result.name == "original.pdf"
    assert result.logical_path == target_path
    assert result.mime_type == "application/pdf"
    assert result.size_bytes == 1024
    assert result.sha256 is None
    assert result.provider_metadata["md5_checksum"] == "d41d8cd98f00b204e9800998ecf8427e"

    files_mock.get.assert_called_once_with(
        fileId="file-rename-abc",
        fields="id,name,mimeType,size,modifiedTime,md5Checksum",
    )


def test_move_no_drive_parent_change(_fake_keyring):
    """Move does not touch parents / folders — only reads metadata."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.get.return_value.execute.return_value = dict(_SAMPLE_FILE_META)

    adapter.move("file-move-xyz", "/New/Folder/doc.txt")

    # Only .get() is called; .update() and .patch() must not be invoked
    files_mock.get.assert_called_once()
    files_mock.update.assert_not_called()
    files_mock.patch.assert_not_called()


def test_move_empty_source_id_raises_not_found(_fake_keyring):
    """Empty or whitespace-only source_provider_file_id is rejected with StorageNotFoundError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()

    for invalid_id in ("", "   ", "\t"):
        with pytest.raises(StorageNotFoundError) as excinfo:
            adapter.move(invalid_id, "/new/path.pdf")
        assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_move_empty_target_path_raises_invalid_response(_fake_keyring):
    """An empty or whitespace-only target_logical_path is rejected before any API call."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()

    for bad_path in ("", "   ", "\t"):
        with pytest.raises(StorageInvalidResponseError) as excinfo:
            adapter.move("file-123", bad_path)
        assert excinfo.value.provider == "GOOGLE_DRIVE"

    adapter._drive_service.files.return_value.get.assert_not_called()


def test_move_unauthenticated_raises_authentication_error():
    """Move without an active Drive service raises StorageAuthenticationError."""
    adapter = GoogleDriveAdapter()

    with pytest.raises(StorageAuthenticationError) as excinfo:
        adapter.move("file-123", "/new/path.pdf")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_move_http_404_maps_to_not_found_error(_fake_keyring):
    """HttpError 404 maps to StorageNotFoundError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.get.side_effect = _make_http_error(404, "File not found")

    with pytest.raises(StorageNotFoundError) as excinfo:
        adapter.move("nonexistent-file", "/new/path.pdf")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_move_http_403_maps_to_permission_error(_fake_keyring):
    """HttpError 403 maps to StoragePermissionError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.get.side_effect = _make_http_error(
        403, "Forbidden", error_reason="forbidden"
    )

    with pytest.raises(StoragePermissionError) as excinfo:
        adapter.move("forbidden-file", "/new/path.pdf")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_move_http_429_maps_to_rate_limit_error(_fake_keyring):
    """HttpError 429 maps to StorageRateLimitError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.get.side_effect = _make_http_error(429, "Too Many Requests")

    with pytest.raises(StorageRateLimitError) as excinfo:
        adapter.move("throttled-file", "/new/path.pdf")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_move_http_500_maps_to_unavailable_error(_fake_keyring):
    """HttpError 500 maps to StorageUnavailableError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.get.side_effect = _make_http_error(500, "Internal Server Error")

    with pytest.raises(StorageUnavailableError) as excinfo:
        adapter.move("server-error-file", "/new/path.pdf")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_move_get_missing_id_raises_invalid_response(_fake_keyring):
    """A Drive get response missing 'id' is rejected with StorageInvalidResponseError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.get.return_value.execute.return_value = {}

    with pytest.raises(StorageInvalidResponseError) as excinfo:
        adapter.move("file-with-no-id", "/new/path.pdf")
    assert excinfo.value.provider == "GOOGLE_DRIVE"
