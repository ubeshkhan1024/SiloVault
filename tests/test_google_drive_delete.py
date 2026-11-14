"""Tests for Google Drive delete lifecycle (M3.2 Phase B1.5).

Covers GoogleDriveAdapter.quarantine(), restore() and cleanup() using a
mocked keyring (same pattern as ``test_google_drive_root_folder.py``) and
a mocked Drive API service — no real Google Drive API calls are made and
no OS credential store is required.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

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
from backend.storage.types import ProviderFileRecord

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
# Tests: quarantine
# ---------------------------------------------------------------------------


def test_quarantine_successful_trashes_file(_fake_keyring):
    """Quarantine sets trashed=True and returns the original file ID."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.update.return_value.execute.return_value = {"id": "file-123"}

    quarantine_id = adapter.quarantine("file-123")

    assert quarantine_id == "file-123"
    files_mock.update.assert_called_once_with(
        fileId="file-123",
        body={"trashed": True},
    )
    # No delete call during quarantine — only trash
    files_mock.delete.assert_not_called()


def test_quarantine_returns_original_id_as_quarantine_id(_fake_keyring):
    """The quarantine ID is exactly the provider file ID (stable across phases)."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.update.return_value.execute.return_value = {}

    assert adapter.quarantine("drive-abc-999") == "drive-abc-999"


def test_quarantine_invalid_file_id_raises_not_found(_fake_keyring):
    """Empty or whitespace-only provider_file_id is rejected before any API call."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()

    for invalid_id in ("", "   ", "\t"):
        with pytest.raises(StorageNotFoundError) as excinfo:
            adapter.quarantine(invalid_id)
        assert excinfo.value.provider == "GOOGLE_DRIVE"

    adapter._drive_service.files.return_value.update.assert_not_called()


def test_quarantine_unauthenticated_raises_authentication_error():
    """Quarantine without an active Drive service raises StorageAuthenticationError."""
    adapter = GoogleDriveAdapter()

    with pytest.raises(StorageAuthenticationError) as excinfo:
        adapter.quarantine("file-123")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_quarantine_http_404_maps_to_not_found_error(_fake_keyring):
    """HttpError 404 during trashing maps to StorageNotFoundError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.update.return_value.execute.side_effect = (
        _make_http_error(404, "File not found")
    )

    with pytest.raises(StorageNotFoundError) as excinfo:
        adapter.quarantine("missing-file")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_quarantine_http_403_maps_to_permission_error(_fake_keyring):
    """HttpError 403 maps to StoragePermissionError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.update.return_value.execute.side_effect = (
        _make_http_error(403, "Forbidden", error_reason="forbidden")
    )

    with pytest.raises(StoragePermissionError) as excinfo:
        adapter.quarantine("forbidden-file")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_quarantine_http_429_maps_to_rate_limit_error(_fake_keyring):
    """HttpError 429 maps to StorageRateLimitError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.update.return_value.execute.side_effect = (
        _make_http_error(429, "Too Many Requests")
    )

    with pytest.raises(StorageRateLimitError) as excinfo:
        adapter.quarantine("throttled-file")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_quarantine_http_500_maps_to_unavailable_error(_fake_keyring):
    """HttpError 500 maps to StorageUnavailableError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.update.return_value.execute.side_effect = (
        _make_http_error(500, "Internal Server Error")
    )

    with pytest.raises(StorageUnavailableError) as excinfo:
        adapter.quarantine("server-error-file")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_quarantine_does_not_delete_file(_fake_keyring):
    """Quarantine must never call files().delete() — only trashing is allowed."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.update.return_value.execute.return_value = {}

    adapter.quarantine("file-123")

    adapter._drive_service.files.return_value.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: restore
# ---------------------------------------------------------------------------


def test_restore_successful_returns_record(_fake_keyring):
    """Restore sets trashed=False and returns a valid ProviderFileRecord."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value

    files_mock.update.return_value.execute.return_value = {}
    files_mock.get.return_value.execute.return_value = {
        "id": "file-restore-1",
        "name": "restored.pdf",
        "mimeType": "application/pdf",
        "size": "2048",
        "modifiedTime": "2024-06-15T10:00:00Z",
        "md5Checksum": "abc123",
    }

    result = adapter.restore("file-restore-1")

    assert isinstance(result, ProviderFileRecord)
    assert result.provider_file_id == "file-restore-1"
    assert result.name == "restored.pdf"
    assert result.logical_path == "/file-restore-1"
    assert result.mime_type == "application/pdf"
    assert result.size_bytes == 2048
    assert result.sha256 is None
    assert result.provider_metadata["md5_checksum"] == "abc123"
    assert result.modified_at is not None

    # Verify trashed=False was sent
    files_mock.update.assert_called_once_with(
        fileId="file-restore-1",
        body={"trashed": False},
    )


def test_restore_invalid_quarantine_id_raises_not_found(_fake_keyring):
    """Empty or whitespace-only quarantine_id is rejected before any API call."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()

    for invalid_id in ("", "   ", "\t"):
        with pytest.raises(StorageNotFoundError) as excinfo:
            adapter.restore(invalid_id)
        assert excinfo.value.provider == "GOOGLE_DRIVE"

    adapter._drive_service.files.return_value.update.assert_not_called()


def test_restore_unauthenticated_raises_authentication_error():
    """Restore without an active Drive service raises StorageAuthenticationError."""
    adapter = GoogleDriveAdapter()

    with pytest.raises(StorageAuthenticationError) as excinfo:
        adapter.restore("file-123")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_restore_http_404_maps_to_not_found_error(_fake_keyring):
    """HttpError 404 during un-trashing maps to StorageNotFoundError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.update.return_value.execute.side_effect = (
        _make_http_error(404, "File not found")
    )

    with pytest.raises(StorageNotFoundError) as excinfo:
        adapter.restore("missing-file")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_restore_http_403_maps_to_permission_error(_fake_keyring):
    """HttpError 403 maps to StoragePermissionError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.update.return_value.execute.side_effect = (
        _make_http_error(403, "Forbidden", error_reason="forbidden")
    )

    with pytest.raises(StoragePermissionError) as excinfo:
        adapter.restore("forbidden-file")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_restore_http_429_maps_to_rate_limit_error(_fake_keyring):
    """HttpError 429 maps to StorageRateLimitError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.update.return_value.execute.side_effect = (
        _make_http_error(429, "Too Many Requests")
    )

    with pytest.raises(StorageRateLimitError) as excinfo:
        adapter.restore("throttled-file")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_restore_http_500_maps_to_unavailable_error(_fake_keyring):
    """HttpError 500 maps to StorageUnavailableError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.update.return_value.execute.side_effect = (
        _make_http_error(500, "Internal Server Error")
    )

    with pytest.raises(StorageUnavailableError) as excinfo:
        adapter.restore("server-error-file")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_restore_get_missing_id_raises_invalid_response(_fake_keyring):
    """A metadata get response missing 'id' is rejected with StorageInvalidResponseError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.update.return_value.execute.return_value = {}
    files_mock.get.return_value.execute.return_value = {}

    with pytest.raises(StorageInvalidResponseError) as excinfo:
        adapter.restore("file-with-no-id")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_restore_does_not_delete_file(_fake_keyring):
    """Restore must never call files().delete()."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.update.return_value.execute.return_value = {}
    files_mock.get.return_value.execute.return_value = {"id": "file-123"}

    adapter.restore("file-123")

    files_mock.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: cleanup
# ---------------------------------------------------------------------------


def test_cleanup_successful_deletes_file(_fake_keyring):
    """Cleanup calls files().delete() with the quarantine ID and returns None."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.delete.return_value.execute.return_value = None

    result = adapter.cleanup("file-cleanup-1")

    assert result is None
    files_mock.delete.assert_called_once_with(fileId="file-cleanup-1")
    # No update call during cleanup
    files_mock.update.assert_not_called()


def test_cleanup_invalid_quarantine_id_raises_not_found(_fake_keyring):
    """Empty or whitespace-only quarantine_id is rejected before any API call."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()

    for invalid_id in ("", "   ", "\t"):
        with pytest.raises(StorageNotFoundError) as excinfo:
            adapter.cleanup(invalid_id)
        assert excinfo.value.provider == "GOOGLE_DRIVE"

    adapter._drive_service.files.return_value.delete.assert_not_called()


def test_cleanup_unauthenticated_raises_authentication_error():
    """Cleanup without an active Drive service raises StorageAuthenticationError."""
    adapter = GoogleDriveAdapter()

    with pytest.raises(StorageAuthenticationError) as excinfo:
        adapter.cleanup("file-123")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_cleanup_http_404_maps_to_not_found_error(_fake_keyring):
    """HttpError 404 (file already permanently gone) maps to StorageNotFoundError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.delete.return_value.execute.side_effect = (
        _make_http_error(404, "File not found")
    )

    with pytest.raises(StorageNotFoundError) as excinfo:
        adapter.cleanup("already-gone-file")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_cleanup_http_403_maps_to_permission_error(_fake_keyring):
    """HttpError 403 maps to StoragePermissionError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.delete.return_value.execute.side_effect = (
        _make_http_error(403, "Forbidden", error_reason="forbidden")
    )

    with pytest.raises(StoragePermissionError) as excinfo:
        adapter.cleanup("forbidden-file")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_cleanup_http_429_maps_to_rate_limit_error(_fake_keyring):
    """HttpError 429 maps to StorageRateLimitError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.delete.return_value.execute.side_effect = (
        _make_http_error(429, "Too Many Requests")
    )

    with pytest.raises(StorageRateLimitError) as excinfo:
        adapter.cleanup("throttled-file")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_cleanup_http_500_maps_to_unavailable_error(_fake_keyring):
    """HttpError 500 maps to StorageUnavailableError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.delete.return_value.execute.side_effect = (
        _make_http_error(500, "Internal Server Error")
    )

    with pytest.raises(StorageUnavailableError) as excinfo:
        adapter.cleanup("server-error-file")
    assert excinfo.value.provider == "GOOGLE_DRIVE"


# ---------------------------------------------------------------------------
# Tests: full three-phase flow
# ---------------------------------------------------------------------------


def test_three_phase_flow_quarantine_restore_cleanup(_fake_keyring):
    """Full delete protocol: quarantine -> restore (DB failure) -> cleanup (success).

    Simulates both recovery paths:
    - restore() after a DB commit failure returns the file to life
    - cleanup() after a successful commit permanently deletes it
    """
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.update.return_value.execute.return_value = {}
    files_mock.get.return_value.execute.return_value = {
        "id": "file-flow-1",
        "name": "flow.pdf",
        "mimeType": "application/pdf",
        "size": "100",
    }

    # Phase 1: quarantine
    quarantine_id = adapter.quarantine("file-flow-1")
    assert quarantine_id == "file-flow-1"
    files_mock.update.assert_called_once_with(
        fileId="file-flow-1",
        body={"trashed": True},
    )

    # Phase 3b (DB failure path): restore
    record = adapter.restore(quarantine_id)
    assert record.provider_file_id == "file-flow-1"
    assert record.name == "flow.pdf"
    assert files_mock.update.call_count == 2
    files_mock.update.assert_called_with(
        fileId="file-flow-1",
        body={"trashed": False},
    )
    # File still exists — no delete yet
    files_mock.delete.assert_not_called()

    # Phase 1 again, then Phase 3a (commit success path): cleanup
    adapter.quarantine("file-flow-1")
    adapter.cleanup(quarantine_id)
    files_mock.delete.assert_called_once_with(fileId="file-flow-1")


def test_cleanup_of_already_deleted_file_raises_not_found(_fake_keyring):
    """A second cleanup of the same file hits 404 and maps to StorageNotFoundError."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    adapter._drive_service.files.return_value.delete.return_value.execute.side_effect = (
        _make_http_error(404, "File not found")
    )

    with pytest.raises(StorageNotFoundError):
        adapter.cleanup("gone-file")
