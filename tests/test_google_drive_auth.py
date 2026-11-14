"""Tests for Google Drive OAuth authentication (M3.1).

All tests use mocked keyring operations via monkeypatch on
``backend.storage.credentials._get_keyring`` so no OS credential store
is required. No real Google credentials are used.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.config import settings
from backend.storage.credentials import (
    CredentialStorageError,
    GoogleDriveCredentials,
    delete_google_drive_credentials,
    has_google_drive_credentials,
    load_google_drive_credentials,
    store_google_drive_credentials,
)


# ---------------------------------------------------------------------------
# Helper: inject a fake keyring into the credentials module
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


# ---------------------------------------------------------------------------
# Tests: credential storage operations
# ---------------------------------------------------------------------------


def test_store_and_load_google_drive_credentials(_fake_keyring):
    """Store refresh token and load it back."""
    creds = GoogleDriveCredentials(
        refresh_token="mock-refresh-token-123",
        client_id="mock-client-id",
        client_secret="mock-client-secret",
        scopes=["https://www.googleapis.com/auth/drive.file"],
    )

    store_google_drive_credentials(creds)
    loaded = load_google_drive_credentials()

    assert loaded is not None
    assert loaded.refresh_token == "mock-refresh-token-123"
    # client_id / client_secret come from config at load time, not from keyring
    assert loaded.client_id == (settings.google_drive_client_id or "")
    assert loaded.client_secret == (settings.google_drive_client_secret or "")
    assert loaded.scopes == ("https://www.googleapis.com/auth/drive.file",)


def test_load_google_drive_credentials_when_absent():
    """Loading credentials when none stored returns None."""
    loaded = load_google_drive_credentials()
    assert loaded is None


def test_delete_google_drive_credentials(_fake_keyring):
    """Delete stored credentials and verify absence."""
    creds = GoogleDriveCredentials(
        refresh_token="to-delete",
        client_id="cid",
        client_secret="cs",
        scopes=["drive.file"],
    )
    store_google_drive_credentials(creds)

    deleted = delete_google_drive_credentials()
    assert deleted is True

    # Now loading should return None
    assert load_google_drive_credentials() is None


def test_has_google_drive_credentials_false_when_absent():
    """has_google_drive_credentials returns False when nothing stored."""
    assert not has_google_drive_credentials()


def test_has_google_drive_credentials_true_after_store(_fake_keyring):
    """has_google_drive_credentials returns True after storing."""
    creds = GoogleDriveCredentials(
        refresh_token="present",
        client_id="cid",
        client_secret="cs",
        scopes=["drive.file"],
    )
    store_google_drive_credentials(creds)
    assert has_google_drive_credentials()


# ---------------------------------------------------------------------------
# Tests: to_dict excludes access_token
# ---------------------------------------------------------------------------


def test_google_drive_credentials_to_dict_excludes_access_token():
    """Ensure to_dict never includes access_token."""
    creds = GoogleDriveCredentials(
        refresh_token="rtoken",
        client_id="cid",
        client_secret="cs",
        scopes=["drive.file"],
    )
    d = creds.to_dict()
    assert "refresh_token" in d
    assert "scopes" in d
    assert "access_token" not in d


# ---------------------------------------------------------------------------
# Tests: config validation
# ---------------------------------------------------------------------------


def test_google_oauth_config_default_scopes():
    """Default OAuth scopes should include drive.file."""
    scopes = settings.google_drive_scopes_list
    assert "https://www.googleapis.com/auth/drive.file" in scopes


# ---------------------------------------------------------------------------
# Tests: integration with keyring module functions
# ---------------------------------------------------------------------------


def test_store_then_load_roundtrip(_fake_keyring):
    """Store credentials, load them, verify roundtrip integrity."""
    original = GoogleDriveCredentials(
        refresh_token="roundtrip-token",
        client_id="cid",
        client_secret="cs",
        scopes=["drive.file"],
    )
    store_google_drive_credentials(original)
    loaded = load_google_drive_credentials()

    assert loaded is not None
    assert loaded.refresh_token == original.refresh_token
    # client_id / client_secret are reconstructed from config on load,
    # not read back from the keyring — assert against config defaults.
    assert loaded.client_id == (settings.google_drive_client_id or "")
    assert loaded.client_secret == (settings.google_drive_client_secret or "")
    assert loaded.scopes == original.scopes


def test_delete_then_load_returns_none(_fake_keyring):
    """Delete credentials, then load should return None."""
    store_google_drive_credentials(
        GoogleDriveCredentials("del-token", "cid", "cs", ["drive.file"])
    )
    delete_google_drive_credentials()
    assert load_google_drive_credentials() is None


def test_stored_data_contains_only_refresh_token_and_scopes(_fake_keyring):
    """Verify only refresh_token and scopes reach the keyring -- client
    credentials come from config at runtime, not from persisted data."""
    creds = GoogleDriveCredentials(
        refresh_token="secret-refresh",
        client_id="cfg-client-id",
        client_secret="cfg-client-secret",
        scopes=["drive.file"],
    )
    store_google_drive_credentials(creds)

    kr = _fake_keyring
    raw = json.loads(kr.get_password("silovault", "google-drive"))
    assert "client_id" not in raw
    assert "client_secret" not in raw
    assert raw["refresh_token"] == "secret-refresh"
    assert raw["scopes"] == ["drive.file"]


def test_load_restores_client_id_from_config(_fake_keyring):
    """Loaded credentials reconstruct client_id/client_secret from config."""
    store_google_drive_credentials(
        GoogleDriveCredentials("rt", "some-id", "some-secret", ["drive.file"])
    )
    loaded = load_google_drive_credentials()
    assert loaded is not None
    assert loaded.client_id == ""  # config default when .env absent
    assert loaded.refresh_token == "rt"
