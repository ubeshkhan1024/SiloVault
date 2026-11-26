"""Tests for Google Drive root-folder management (M3.2 Phase B1.1).

All tests use a mocked keyring (same pattern as ``test_google_drive_auth.py``)
and a mocked Drive API service — no real Google Drive API calls are made and
no OS credential store is required.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from backend.storage.credentials import (
    CredentialStorageError,
    GoogleDriveCredentials,
    load_google_drive_credentials,
    store_google_drive_credentials,
)
from backend.storage.errors import (
    StorageAuthenticationError,
    StorageError,
    StorageNotFoundError,
    StoragePermissionError,
    StorageQuotaExceededError,
    StorageRateLimitError,
    StorageUnavailableError,
)
from backend.storage.google_drive import (
    SILOVAULT_ROOT_FOLDER_NAME,
    GoogleDriveAdapter,
)

_KEYRING_SERVICE = "silovault"
_KEYRING_ACCOUNT = "google-drive"
_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


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


def _seed_credentials(refresh_token: str = "rt-123") -> None:
    """Store valid M3.1-format credentials in the (fake) keyring."""
    store_google_drive_credentials(
        GoogleDriveCredentials(
            refresh_token=refresh_token,
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
# Tests: discovery and creation
# ---------------------------------------------------------------------------


def test_reuses_existing_accessible_silovault_folder(_fake_keyring):
    """An existing SiloVault folder is discovered and reused, not duplicated."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.list.return_value.execute.return_value = {
        "files": [{"id": "existing-123"}]
    }

    folder_id = adapter.get_root_folder_id()

    assert folder_id == "existing-123"
    files_mock.list.assert_called_once()
    files_mock.create.assert_not_called()


def test_create_folder_when_no_matching_folder_exists(_fake_keyring):
    """A SiloVault folder is created when discovery finds none."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.list.return_value.execute.return_value = {"files": []}
    files_mock.create.return_value.execute.return_value = {"id": "new-folder-456"}

    folder_id = adapter.get_root_folder_id()

    assert folder_id == "new-folder-456"
    files_mock.create.assert_called_once_with(
        body={
            "name": SILOVAULT_ROOT_FOLDER_NAME,
            "mimeType": _FOLDER_MIME_TYPE,
        },
        fields="id",
    )


def test_discovery_query_targets_silovault_folder(_fake_keyring):
    """The discovery query matches the SiloVault folder by name and type."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.list.return_value.execute.return_value = {
        "files": [{"id": "existing-123"}]
    }

    adapter.get_root_folder_id()

    call_kwargs = files_mock.list.call_args.kwargs
    assert SILOVAULT_ROOT_FOLDER_NAME in call_kwargs["q"]
    assert _FOLDER_MIME_TYPE in call_kwargs["q"]
    assert "trashed=false" in call_kwargs["q"]
    assert call_kwargs["spaces"] == "drive"


def test_cached_root_folder_id_reused_without_api_lookup(_fake_keyring):
    """A second call returns the cached ID without further Drive API calls."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.list.return_value.execute.return_value = {
        "files": [{"id": "existing-123"}]
    }

    first = adapter.get_root_folder_id()
    second = adapter.get_root_folder_id()

    assert first == second == "existing-123"
    assert files_mock.list.call_count == 1
    assert files_mock.create.call_count == 0


def test_persisted_root_folder_id_skips_discovery(_fake_keyring):
    """A root folder ID already in the keyring is used without any API call."""
    _seed_credentials()
    store_google_drive_credentials(
        load_google_drive_credentials().with_root_folder_id("stored-folder-789")
    )
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value

    folder_id = adapter.get_root_folder_id()

    assert folder_id == "stored-folder-789"
    files_mock.list.assert_not_called()
    files_mock.create.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: persistence
# ---------------------------------------------------------------------------


def test_persists_folder_id_preserving_existing_fields(_fake_keyring):
    """Discovery result is persisted via with_root_folder_id(); the refresh
    token and scopes in the keyring record are preserved verbatim."""
    _seed_credentials(refresh_token="original-rt")
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.list.return_value.execute.return_value = {"files": []}
    files_mock.create.return_value.execute.return_value = {"id": "new-folder-456"}

    folder_id = adapter.get_root_folder_id()

    assert folder_id == "new-folder-456"
    assert adapter._root_folder_persistence_pending is False
    raw = json.loads(_fake_keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT))
    assert raw["root_folder_id"] == "new-folder-456"
    assert raw["refresh_token"] == "original-rt"
    assert raw["scopes"] == [_DRIVE_FILE_SCOPE]


def test_persistence_failure_keeps_cached_id_and_keyring_untouched(
    _fake_keyring, monkeypatch: pytest.MonkeyPatch
):
    """When the keyring write fails, the cached folder ID is retained, the
    pending flag is set, and the existing keyring record is not modified."""
    _seed_credentials(refresh_token="original-rt")
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.list.return_value.execute.return_value = {"files": []}
    files_mock.create.return_value.execute.return_value = {"id": "new-folder-456"}

    def failing_store(credentials: GoogleDriveCredentials) -> None:
        raise CredentialStorageError("keyring write failed")

    monkeypatch.setattr(
        "backend.storage.google_drive.store_google_drive_credentials", failing_store
    )

    folder_id = adapter.get_root_folder_id()

    assert folder_id == "new-folder-456"
    assert adapter._root_folder_id == "new-folder-456"
    assert adapter._root_folder_persistence_pending is True
    raw = json.loads(_fake_keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT))
    assert "root_folder_id" not in raw
    assert raw["refresh_token"] == "original-rt"


def test_persistence_retried_on_subsequent_operation(
    _fake_keyring, monkeypatch: pytest.MonkeyPatch
):
    """A failed persistence is retried on the next get_root_folder_id() call
    without re-running discovery, and the pending flag clears on success."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.list.return_value.execute.return_value = {"files": []}
    files_mock.create.return_value.execute.return_value = {"id": "new-folder-456"}

    state = {"fail": True}
    original_store = store_google_drive_credentials

    def flaky_store(credentials: GoogleDriveCredentials) -> None:
        if state["fail"]:
            raise CredentialStorageError("keyring write failed")
        original_store(credentials)

    monkeypatch.setattr(
        "backend.storage.google_drive.store_google_drive_credentials", flaky_store
    )

    # First call: discovery + failed persistence.
    assert adapter.get_root_folder_id() == "new-folder-456"
    assert adapter._root_folder_persistence_pending is True
    raw = json.loads(_fake_keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT))
    assert "root_folder_id" not in raw

    # Second call: cache hit triggers the persistence retry only.
    state["fail"] = False
    assert adapter.get_root_folder_id() == "new-folder-456"
    assert adapter._root_folder_persistence_pending is False
    raw = json.loads(_fake_keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT))
    assert raw["root_folder_id"] == "new-folder-456"

    # No additional Drive API traffic during the retry.
    assert files_mock.list.call_count == 1
    assert files_mock.create.call_count == 1


def test_backwards_compatible_with_credentials_without_root_folder_id(
    _fake_keyring,
):
    """An M3.1 keyring record (no root_folder_id key) still works: the folder
    is discovered and the ID persisted alongside the existing record."""
    # Raw M3.1-format record: refresh_token + scopes, no root_folder_id.
    _fake_keyring.set_password(
        _KEYRING_SERVICE,
        _KEYRING_ACCOUNT,
        json.dumps({"refresh_token": "legacy-rt", "scopes": [_DRIVE_FILE_SCOPE]}),
    )
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.list.return_value.execute.return_value = {
        "files": [{"id": "existing-123"}]
    }

    folder_id = adapter.get_root_folder_id()

    assert folder_id == "existing-123"
    raw = json.loads(_fake_keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT))
    assert raw["root_folder_id"] == "existing-123"
    assert raw["refresh_token"] == "legacy-rt"


# ---------------------------------------------------------------------------
# Tests: error handling
# ---------------------------------------------------------------------------


def test_lookup_failure_does_not_create_duplicate_folder(_fake_keyring):
    """A failed folder lookup raises and never falls through to creation."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.list.return_value.execute.side_effect = _make_http_error(
        403, "Forbidden", error_reason="forbidden"
    )

    with pytest.raises(StoragePermissionError):
        adapter.get_root_folder_id()

    files_mock.create.assert_not_called()
    assert adapter._root_folder_id is None
    assert adapter._root_folder_persistence_pending is False


def test_http_404_maps_to_storage_not_found(_fake_keyring):
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.list.return_value.execute.side_effect = _make_http_error(404, "Not Found")

    with pytest.raises(StorageNotFoundError) as excinfo:
        adapter.get_root_folder_id()

    assert excinfo.value.provider == "GOOGLE_DRIVE"
    files_mock.create.assert_not_called()


def test_http_429_maps_to_rate_limit_error(_fake_keyring):
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.list.return_value.execute.return_value = {"files": []}
    files_mock.create.return_value.execute.side_effect = _make_http_error(429, "Too Many Requests")

    with pytest.raises(StorageRateLimitError) as excinfo:
        adapter.get_root_folder_id()

    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_http_403_rate_limit_reason_maps_to_rate_limit_error(_fake_keyring):
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.list.return_value.execute.side_effect = _make_http_error(
        403, "Forbidden", error_reason="userRateLimitExceeded"
    )

    with pytest.raises(StorageRateLimitError):
        adapter.get_root_folder_id()


def test_http_413_maps_to_quota_exceeded_error(_fake_keyring):
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.list.return_value.execute.return_value = {"files": []}
    files_mock.create.return_value.execute.side_effect = _make_http_error(
        413, "Request Entity Too Large", error_reason="insufficientStorage"
    )

    with pytest.raises(StorageQuotaExceededError):
        adapter.get_root_folder_id()


def test_http_500_maps_to_unavailable_error(_fake_keyring):
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.list.return_value.execute.return_value = {"files": []}
    files_mock.create.return_value.execute.side_effect = _make_http_error(500, "Internal Server Error")

    with pytest.raises(StorageUnavailableError) as excinfo:
        adapter.get_root_folder_id()

    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_missing_id_in_create_response_is_rejected(_fake_keyring):
    """A create response without an 'id' field is a protocol violation."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.list.return_value.execute.return_value = {"files": []}
    files_mock.create.return_value.execute.return_value = {}

    with pytest.raises(StorageError):
        adapter.get_root_folder_id()

    assert adapter._root_folder_id is None


def test_unauthenticated_adapter_raises_authentication_error():
    """A fresh adapter without a Drive service rejects root-folder requests."""
    adapter = GoogleDriveAdapter()

    with pytest.raises(StorageAuthenticationError) as excinfo:
        adapter.get_root_folder_id()

    assert excinfo.value.provider == "GOOGLE_DRIVE"


def test_keyring_read_failure_raises_unavailable_error(
    monkeypatch: pytest.MonkeyPatch,
):
    """A keyring read failure surfaces as StorageUnavailableError; the
    adapter never guesses or overwrites durable state."""
    adapter = _make_authenticated_adapter()

    def failing_load():
        raise CredentialStorageError("keyring read failed")

    monkeypatch.setattr(
        "backend.storage.google_drive.load_google_drive_credentials", failing_load
    )

    with pytest.raises(StorageUnavailableError) as excinfo:
        adapter.get_root_folder_id()

    assert excinfo.value.provider == "GOOGLE_DRIVE"
    adapter._drive_service.files.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: lifecycle
# ---------------------------------------------------------------------------


def test_disconnect_clears_root_folder_state(_fake_keyring):
    """disconnect() clears the cached ID and the pending persistence flag."""
    _seed_credentials()
    adapter = _make_authenticated_adapter()
    files_mock = adapter._drive_service.files.return_value
    files_mock.list.return_value.execute.return_value = {
        "files": [{"id": "existing-123"}]
    }

    assert adapter.get_root_folder_id() == "existing-123"
    adapter.disconnect()

    assert adapter._root_folder_id is None
    assert adapter._root_folder_persistence_pending is False
    assert adapter._drive_service is None
    with pytest.raises(StorageAuthenticationError):
        adapter.get_root_folder_id()


# ---------------------------------------------------------------------------
# Tests: account-scoped isolation (HVSA H1.4C)
# ---------------------------------------------------------------------------


def _ref_account_name(credentials_ref: str) -> str:
    """Keyring account name derived from a credential reference."""
    from backend.storage.credentials import _get_credentials_account_name

    return _get_credentials_account_name(credentials_ref)


def _seed_scoped_credentials(credentials_ref: str, refresh_token: str) -> None:
    store_google_drive_credentials(
        GoogleDriveCredentials(
            refresh_token=refresh_token,
            client_id="cid",
            client_secret="cs",
            scopes=[_DRIVE_FILE_SCOPE],
        ),
        credentials_ref=credentials_ref,
    )


def _make_scoped_adapter(credentials_ref: str) -> GoogleDriveAdapter:
    adapter = GoogleDriveAdapter(credentials_ref=credentials_ref)
    adapter._drive_service = MagicMock()
    return adapter


def test_scoped_adapter_uses_its_own_persisted_root_folder(_fake_keyring):
    """An account-scoped adapter reuses the root folder stored under its ref."""
    store_google_drive_credentials(
        GoogleDriveCredentials(
            refresh_token="rt-a",
            client_id="cid",
            client_secret="cs",
            scopes=[_DRIVE_FILE_SCOPE],
            root_folder_id="folder-a",
        ),
        credentials_ref="acct-a",
    )
    adapter = _make_scoped_adapter("acct-a")
    files_mock = adapter._drive_service.files.return_value

    assert adapter.get_root_folder_id() == "folder-a"
    files_mock.list.assert_not_called()
    files_mock.create.assert_not_called()


def test_scoped_adapter_ignores_legacy_entry(_fake_keyring):
    """A scoped adapter never falls back to the legacy global credential entry."""
    _seed_credentials(refresh_token="legacy-rt")
    store_google_drive_credentials(
        load_google_drive_credentials().with_root_folder_id("legacy-folder")
    )
    _seed_scoped_credentials("acct-a", "rt-a")

    adapter = _make_scoped_adapter("acct-a")
    files_mock = adapter._drive_service.files.return_value
    files_mock.list.return_value.execute.return_value = {"files": []}
    files_mock.create.return_value.execute.return_value = {"id": "folder-a"}

    # Legacy root folder ID must not leak into the scoped account.
    assert adapter.get_root_folder_id() == "folder-a"
    files_mock.list.assert_called_once()


def test_scoped_persistence_writes_only_its_own_entry(_fake_keyring):
    """Root-folder persistence is written under the account's own keyring entry."""
    _seed_credentials(refresh_token="legacy-rt")
    _seed_scoped_credentials("acct-a", "rt-a")

    adapter = _make_scoped_adapter("acct-a")
    files_mock = adapter._drive_service.files.return_value
    files_mock.list.return_value.execute.return_value = {"files": []}
    files_mock.create.return_value.execute.return_value = {"id": "folder-a"}

    assert adapter.get_root_folder_id() == "folder-a"

    scoped_raw = json.loads(
        _fake_keyring.get_password(_KEYRING_SERVICE, _ref_account_name("acct-a"))
    )
    assert scoped_raw["root_folder_id"] == "folder-a"
    assert scoped_raw["refresh_token"] == "rt-a"

    legacy_raw = json.loads(
        _fake_keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
    )
    assert legacy_raw["refresh_token"] == "legacy-rt"
    assert "root_folder_id" not in legacy_raw


def test_two_scoped_adapters_have_isolated_root_folders(_fake_keyring):
    """Account A and Account B each resolve and persist their own root folder."""
    store_google_drive_credentials(
        GoogleDriveCredentials(
            refresh_token="rt-a",
            client_id="cid",
            client_secret="cs",
            scopes=[_DRIVE_FILE_SCOPE],
            root_folder_id="folder-a",
        ),
        credentials_ref="acct-a",
    )
    store_google_drive_credentials(
        GoogleDriveCredentials(
            refresh_token="rt-b",
            client_id="cid",
            client_secret="cs",
            scopes=[_DRIVE_FILE_SCOPE],
            root_folder_id="folder-b",
        ),
        credentials_ref="acct-b",
    )

    adapter_a = _make_scoped_adapter("acct-a")
    adapter_b = _make_scoped_adapter("acct-b")

    assert adapter_a.get_root_folder_id() == "folder-a"
    assert adapter_b.get_root_folder_id() == "folder-b"

    # Cross-account isolation: neither adapter saw the other's folder.
    adapter_a._drive_service.files.return_value.list.assert_not_called()
    adapter_b._drive_service.files.return_value.list.assert_not_called()


def test_scoped_adapter_without_own_credentials_does_not_create_legacy_entry(
    _fake_keyring,
):
    """A scoped adapter with no stored credentials never invents a folder ID
    and never writes the legacy (unscoped) entry."""
    _seed_credentials(refresh_token="legacy-rt")
    adapter = _make_scoped_adapter("acct-a")
    files_mock = adapter._drive_service.files.return_value
    files_mock.list.return_value.execute.return_value = {"files": []}
    files_mock.create.return_value.execute.return_value = {"id": "folder-a"}

    # Discovery still works, but persistence fails safely (no scoped record).
    assert adapter.get_root_folder_id() == "folder-a"
    assert adapter._root_folder_persistence_pending is True

    legacy_raw = json.loads(
        _fake_keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
    )
    assert legacy_raw["refresh_token"] == "legacy-rt"
    assert "root_folder_id" not in legacy_raw


def test_disconnect_keeps_stored_credentials_for_all_accounts(_fake_keyring):
    """Disconnecting one adapter must not delete any stored credentials."""
    from backend.storage.credentials import (
        has_google_drive_credentials,
        load_google_drive_credentials,
    )

    _seed_credentials(refresh_token="legacy-rt")
    _seed_scoped_credentials("acct-a", "rt-a")
    _seed_scoped_credentials("acct-b", "rt-b")

    adapter_a = _make_scoped_adapter("acct-a")
    adapter_a._root_folder_id = "folder-a"
    adapter_a.disconnect()

    assert adapter_a._credentials_ref == "acct-a"
    assert load_google_drive_credentials(credentials_ref="acct-a") is not None
    assert load_google_drive_credentials(credentials_ref="acct-b") is not None
    assert load_google_drive_credentials() is not None
    assert has_google_drive_credentials(credentials_ref="acct-a") is True
