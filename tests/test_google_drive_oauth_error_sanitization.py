"""Legacy Google Drive OAuth error sanitization (HVSA H1.5.4 Step 7, Part B).

``_establish_connection`` used to return ``"<code>: <exception text>"`` and the
legacy ``POST /api/v1/google-drive/connect`` echoed that string in a 500 body.
Those details can contain internal account identifiers, credential references,
tokens or filesystem paths, so the flow now returns the stable error code only
and the endpoint answers with a fixed message.

These tests use fake OAuth flows and fake adapters that raise errors containing
recognizable secret markers, and assert none of those markers reach a response.
No real Google credentials, browser flow or keyring are involved.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api.v1.endpoints import google_drive as gd_endpoints
from backend.config import settings
from backend.main import app
from backend.storage.credentials import CredentialStorageError
from backend.storage.errors import StorageAuthenticationError, StorageUnavailableError
from backend.storage.google_drive import GoogleDriveAdapter

#: Markers that must never appear in a client-visible response.
MARKER_ACCOUNT = "acct-9f2c-SECRET-ACCOUNT"
MARKER_REF = "storage-account:super-secret-ref"
MARKER_TOKEN = "ya29.SECRET-ACCESS-TOKEN"
MARKER_PATH = "/srv/private/keyring/secret-entry"

MARKERS = (MARKER_ACCOUNT, MARKER_REF, MARKER_TOKEN, MARKER_PATH)

LEAKY_DETAIL = (
    f"account={MARKER_ACCOUNT} ref={MARKER_REF} "
    f"token={MARKER_TOKEN} keyring={MARKER_PATH}"
)


@pytest.fixture
def client(monkeypatch):
    """Legacy connect endpoint with configured OAuth settings and clean state."""
    monkeypatch.setattr(settings, "google_drive_client_id", "test-client-id")
    monkeypatch.setattr(settings, "google_drive_client_secret", "test-client-secret")
    gd_endpoints._set_adapter(None)
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        gd_endpoints._set_adapter(None)


@pytest.fixture
def fake_credentials(monkeypatch):
    """Keep the flow off the OS keyring."""
    stored: list[tuple] = []

    def _store(creds, credentials_ref=None):
        stored.append((creds.refresh_token, credentials_ref))

    def _delete(credentials_ref=None):
        return True

    monkeypatch.setattr(gd_endpoints, "store_google_drive_credentials", _store)
    monkeypatch.setattr(gd_endpoints, "delete_google_drive_credentials", _delete)
    return stored


def _assert_no_markers(text: str) -> None:
    for marker in MARKERS:
        assert marker not in text


# ---------------------------------------------------------------------------
# Flow result: stable codes only
# ---------------------------------------------------------------------------


def test_provider_unavailable_returns_code_only(monkeypatch, fake_credentials):
    def _offline(self, config):
        raise StorageUnavailableError(detail=LEAKY_DETAIL, provider="GOOGLE_DRIVE")

    monkeypatch.setattr(GoogleDriveAdapter, "authenticate", _offline)

    result = gd_endpoints._establish_connection("token-a", ["scope-a"])

    assert result["success"] is False
    assert result["error"] == "provider_unavailable"
    _assert_no_markers(repr(result))


def test_authentication_failure_returns_code_only(monkeypatch, fake_credentials):
    def _rejected(self, config):
        raise StorageAuthenticationError(detail=LEAKY_DETAIL, provider="GOOGLE_DRIVE")

    monkeypatch.setattr(GoogleDriveAdapter, "authenticate", _rejected)

    result = gd_endpoints._establish_connection("token-a", ["scope-a"])

    assert result["success"] is False
    assert result["error"] == "authentication_failed"
    _assert_no_markers(repr(result))


def test_credential_storage_failure_returns_code_only(monkeypatch):
    def _locked(creds, credentials_ref=None):
        raise CredentialStorageError(LEAKY_DETAIL)

    monkeypatch.setattr(gd_endpoints, "store_google_drive_credentials", _locked)

    result = gd_endpoints._establish_connection("token-a", ["scope-a"])

    assert result["success"] is False
    assert result["error"] == "credential_storage_failed"
    _assert_no_markers(repr(result))


# ---------------------------------------------------------------------------
# Legacy endpoint responses
# ---------------------------------------------------------------------------


def test_connect_provider_unavailable_is_generic(client, monkeypatch):
    monkeypatch.setattr(
        gd_endpoints,
        "_run_oauth_flow",
        lambda **kwargs: {"success": False, "error": "provider_unavailable"},
    )

    response = client.post("/api/v1/google-drive/connect")

    assert response.status_code == 500
    assert response.json()["detail"] == "OAuth flow failed."
    assert "provider_unavailable" not in response.text
    _assert_no_markers(response.text)


def test_connect_authentication_failure_is_generic(client, monkeypatch):
    monkeypatch.setattr(
        gd_endpoints,
        "_run_oauth_flow",
        lambda **kwargs: {"success": False, "error": "authentication_failed"},
    )

    response = client.post("/api/v1/google-drive/connect")

    assert response.status_code == 500
    assert response.json()["detail"] == "OAuth flow failed."
    assert "authentication_failed" not in response.text


def test_connect_unexpected_exception_is_generic(client, monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError(LEAKY_DETAIL)

    monkeypatch.setattr(gd_endpoints, "_run_oauth_flow", _boom)

    response = client.post("/api/v1/google-drive/connect")

    assert response.status_code == 500
    assert response.json()["detail"] == "OAuth flow failed."
    _assert_no_markers(response.text)


def test_connect_real_flow_provider_failure_leaks_nothing(
    client, monkeypatch, fake_credentials
):
    """The real ``_establish_connection`` path must not leak through the endpoint."""

    def _offline(self, config):
        raise StorageUnavailableError(detail=LEAKY_DETAIL, provider="GOOGLE_DRIVE")

    monkeypatch.setattr(GoogleDriveAdapter, "authenticate", _offline)
    monkeypatch.setattr(
        gd_endpoints,
        "_run_oauth_flow",
        lambda **kwargs: gd_endpoints._establish_connection("token-a", ["scope-a"]),
    )

    response = client.post("/api/v1/google-drive/connect")

    assert response.status_code == 500
    assert response.json()["detail"] == "OAuth flow failed."
    assert "provider_unavailable" not in response.text
    _assert_no_markers(response.text)


# ---------------------------------------------------------------------------
# Preserved contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error,status,detail",
    [
        ("state_mismatch", 400, "OAuth state validation failed."),
        ("timeout", 408, "OAuth flow timed out."),
        (
            "no_refresh_token",
            400,
            "Google did not return a refresh token. "
            "Ensure 'offline' access type is configured.",
        ),
    ],
)
def test_connect_known_flow_errors_keep_status_and_message(
    client, monkeypatch, error, status, detail
):
    monkeypatch.setattr(
        gd_endpoints,
        "_run_oauth_flow",
        lambda **kwargs: {"success": False, "error": error},
    )

    response = client.post("/api/v1/google-drive/connect")

    assert response.status_code == status
    assert response.json()["detail"] == detail


def test_connect_success_is_unchanged(client, monkeypatch):
    monkeypatch.setattr(
        gd_endpoints,
        "_run_oauth_flow",
        lambda **kwargs: {"success": True, "error": None},
    )

    response = client.post("/api/v1/google-drive/connect")

    assert response.status_code == 200
    assert response.json()["status"] == "connected"


def test_managed_flow_error_mapping_unchanged(monkeypatch):
    """``_raise_oauth_failure`` still maps the stable codes and logs no detail."""
    from fastapi import HTTPException

    from backend.api.v1.endpoints import storage_accounts

    with pytest.raises(HTTPException) as excinfo:
        storage_accounts._raise_oauth_failure(
            f"provider_unavailable: {LEAKY_DETAIL}", "acct-id"
        )

    assert excinfo.value.status_code == 500
    assert excinfo.value.detail == "OAuth flow failed."
    _assert_no_markers(str(excinfo.value.detail))
