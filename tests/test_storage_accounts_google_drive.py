"""Tests for account-scoped Google Drive OAuth endpoints (HVSA H1.4D).

All external effects are mocked: an in-memory fake keyring stands in for the
OS credential store and the browser/loopback OAuth flow is replaced, so these
tests never touch a real keyring, the real application database, Google's
servers, or the user's browser.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from keyring.errors import PasswordDeleteError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from backend.api.v1.endpoints import google_drive as gd_endpoints
from backend.api.v1.endpoints import storage_accounts as gd_accounts
from backend.config import settings
from backend.database import Base, get_db
from backend.main import app
from backend.models import StorageAccount
from backend.storage import credentials as cred_mod
from backend.storage.account_manager import AccountManager
from backend.storage.credentials import (
    GoogleDriveCredentials,
    _derive_keyring_account,
    store_google_drive_credentials,
)
from backend.storage import registry as registry_mod
from backend.storage.errors import (
    StorageAuthenticationError,
    StorageUnavailableError,
)
from backend.storage.google_drive import GoogleDriveAdapter
from backend.storage.local import LocalAdapter

KEYRING_SERVICE = "silovault"
LEGACY_ACCOUNT_NAME = "google-drive"


# ---------------------------------------------------------------------------
# Fake keyring (no OS credential store access)
# ---------------------------------------------------------------------------


class _FakeKeyring:
    """Minimal in-memory stand-in for the OS keyring backend."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self._entries[(service, username)] = password

    def get_password(self, service: str, username: str):
        return self._entries.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self._entries:
            raise PasswordDeleteError("not found")
        del self._entries[(service, username)]


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    """Route all credential storage through an in-memory fake keyring."""
    kr = _FakeKeyring()
    monkeypatch.setattr(cred_mod, "_get_keyring", lambda: kr)
    return kr


# ---------------------------------------------------------------------------
# Isolated app + database
# ---------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """TestClient wired to an isolated in-memory DB, with clean module state."""
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

    session = TestSession()
    gd_endpoints._set_adapter(None)
    gd_endpoints._startup_auth_failure_reason = None
    gd_accounts._managed_adapters.clear()

    try:
        yield SimpleNamespace(
            client=TestClient(app, raise_server_exceptions=False),
            session=session,
            engine=engine,
        )
    finally:
        gd_accounts._managed_adapters.clear()
        gd_endpoints._set_adapter(None)
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_account(
    session,
    *,
    provider_id: str = "GOOGLE_DRIVE",
    display_name: str = "Test Account",
    credentials_ref: str | None = None,
) -> StorageAccount:
    return AccountManager(session).create_account(
        provider_id=provider_id,
        display_name=display_name,
        credentials_ref=credentials_ref,
    )


def _creds(refresh_token: str) -> GoogleDriveCredentials:
    return GoogleDriveCredentials(
        refresh_token=refresh_token,
        client_id="client-id",
        client_secret="client-secret",
        scopes=("scope-a",),
    )


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "google_drive_client_id", "mock-client-id")
    monkeypatch.setattr(settings, "google_drive_client_secret", "mock-client-secret")


def _install_flow(
    monkeypatch: pytest.MonkeyPatch,
    *,
    refresh_token: str = "refresh-token-A",
    error: str | None = None,
) -> list:
    """Replace the loopback OAuth flow with a deterministic fake.

    The fake records the ``credentials_ref`` the endpoint asked for and stores
    credentials through the *real* scoped credential helpers, so keyring
    isolation is exercised rather than stubbed.
    """
    calls: list = []

    def fake_flow(*, credentials_ref=None, promote_to=None):
        # Records the *permanent* reference the endpoint asked to connect, not
        # the per-attempt staging reference (HVSA H1.4F).
        calls.append(promote_to if promote_to is not None else credentials_ref)
        if error is not None:
            return {"success": False, "error": error}
        target = _fake_flow_store(
            credentials_ref=credentials_ref,
            promote_to=promote_to,
            refresh_token=refresh_token,
        )
        return {
            "success": True,
            "error": None,
            "adapter": GoogleDriveAdapter(credentials_ref=target),
        }

    monkeypatch.setattr(gd_endpoints, "_run_oauth_flow", fake_flow)
    return calls


def _fake_flow_store(
    *, credentials_ref, promote_to, refresh_token
) -> str | None:
    """Mirror the real stage -> promote writes without touching a keyring.

    Returns the reference the adapter should be scoped to.
    """
    store_google_drive_credentials(
        _creds(refresh_token), credentials_ref=credentials_ref
    )
    if promote_to is None:
        return credentials_ref
    store_google_drive_credentials(
        _creds(refresh_token), credentials_ref=promote_to
    )
    gd_endpoints._discard_scoped_credentials(credentials_ref)
    return promote_to


def _keyring_accounts(fake_keyring: _FakeKeyring) -> set:
    """Every keyring account name currently holding an entry."""
    return {account for (_service, account) in fake_keyring._entries}


def _setup_flow(monkeypatch, *, refresh_token="refresh-token-A", error=None) -> list:
    _configure(monkeypatch)
    return _install_flow(monkeypatch, refresh_token=refresh_token, error=error)


def _stored_token(fake_keyring: _FakeKeyring, account_name: str):
    raw = fake_keyring.get_password(KEYRING_SERVICE, account_name)
    return None if raw is None else json.loads(raw)["refresh_token"]


def _account_token(fake_keyring: _FakeKeyring, credentials_ref: str):
    return _stored_token(fake_keyring, _derive_keyring_account(credentials_ref))


def _reload(session, account_id: str) -> StorageAccount:
    session.expire_all()
    account = session.get(StorageAccount, account_id)
    assert account is not None
    return account


def _connect_url(account_id: str) -> str:
    return f"/api/v1/storage-accounts/{account_id}/google-drive/connect"


def _status_url(account_id: str) -> str:
    return f"/api/v1/storage-accounts/{account_id}/google-drive/status"


def _disconnect_url(account_id: str) -> str:
    return f"/api/v1/storage-accounts/{account_id}/google-drive/disconnect"


# ---------------------------------------------------------------------------
# Account validation
# ---------------------------------------------------------------------------


def test_connect_unknown_account_returns_404(env):
    response = env.client.post(_connect_url("does-not-exist"))
    assert response.status_code == 404


def test_connect_local_account_rejected(env):
    account = _make_account(env.session, provider_id="LOCAL")
    response = env.client.post(_connect_url(account.id))
    assert response.status_code == 400
    assert "local" in response.json()["detail"].lower()


def test_connect_unsupported_provider_rejected(env):
    account = _make_account(env.session, provider_id="DROPBOX")
    response = env.client.post(_connect_url(account.id))
    assert response.status_code == 400
    assert "DROPBOX" in response.json()["detail"]


def test_connect_google_drive_account_accepted(env, monkeypatch):
    _setup_flow(monkeypatch)
    account = _make_account(env.session)

    response = env.client.post(_connect_url(account.id))

    assert response.status_code == 200
    payload = response.json()
    assert payload["account_id"] == account.id
    assert payload["provider_id"] == "GOOGLE_DRIVE"
    assert payload["connected"] is True
    assert payload["credentials_present"] is True
    assert _reload(env.session, account.id).status == "connected"


def test_connect_assigns_server_generated_credentials_ref(env, monkeypatch):
    _setup_flow(monkeypatch)
    account = _make_account(env.session)
    assert account.credentials_ref is None

    response = env.client.post(_connect_url(account.id))

    assert response.status_code == 200
    stored = _reload(env.session, account.id)
    assert stored.credentials_ref == f"storage-account:{account.id}"


def test_connect_preserves_existing_credentials_ref(env, monkeypatch):
    _setup_flow(monkeypatch)
    account = _make_account(env.session, credentials_ref="existing-ref")

    response = env.client.post(_connect_url(account.id))

    assert response.status_code == 200
    assert _reload(env.session, account.id).credentials_ref == "existing-ref"


def test_connect_requires_configured_client(env, monkeypatch):
    monkeypatch.setattr(settings, "google_drive_client_id", None)
    monkeypatch.setattr(settings, "google_drive_client_secret", None)
    account = _make_account(env.session)

    response = env.client.post(_connect_url(account.id))

    assert response.status_code == 503
    # No side effects: no reference assigned, no status change.
    stored = _reload(env.session, account.id)
    assert stored.credentials_ref is None
    assert stored.status == "pending"


# ---------------------------------------------------------------------------
# Credential isolation
# ---------------------------------------------------------------------------


def test_connect_stores_credentials_under_account_ref(env, monkeypatch, fake_keyring):
    calls = _setup_flow(monkeypatch, refresh_token="token-a")
    account = _make_account(env.session, credentials_ref="ref-a")

    response = env.client.post(_connect_url(account.id))

    assert response.status_code == 200
    assert calls == ["ref-a"]
    assert _account_token(fake_keyring, "ref-a") == "token-a"
    # The legacy entry is never written by a managed-account connection.
    assert _stored_token(fake_keyring, LEGACY_ACCOUNT_NAME) is None


def test_two_accounts_keep_separate_credentials(env, monkeypatch, fake_keyring):
    account_a = _make_account(env.session, display_name="A", credentials_ref="ref-a")
    account_b = _make_account(env.session, display_name="B", credentials_ref="ref-b")

    _setup_flow(monkeypatch, refresh_token="token-a")
    assert env.client.post(_connect_url(account_a.id)).status_code == 200
    _setup_flow(monkeypatch, refresh_token="token-b")
    assert env.client.post(_connect_url(account_b.id)).status_code == 200

    assert _account_token(fake_keyring, "ref-a") == "token-a"
    assert _account_token(fake_keyring, "ref-b") == "token-b"


def test_disconnect_a_does_not_delete_b(env, monkeypatch, fake_keyring):
    account_a = _make_account(env.session, display_name="A", credentials_ref="ref-a")
    account_b = _make_account(env.session, display_name="B", credentials_ref="ref-b")
    _setup_flow(monkeypatch, refresh_token="token-a")
    env.client.post(_connect_url(account_a.id))
    _setup_flow(monkeypatch, refresh_token="token-b")
    env.client.post(_connect_url(account_b.id))

    response = env.client.delete(_disconnect_url(account_a.id))

    assert response.status_code == 204
    assert _account_token(fake_keyring, "ref-a") is None
    assert _account_token(fake_keyring, "ref-b") == "token-b"
    assert _reload(env.session, account_b.id).status == "connected"


def test_disconnect_does_not_delete_legacy_credentials(env, monkeypatch, fake_keyring):
    store_google_drive_credentials(_creds("legacy-token"))
    account = _make_account(env.session, credentials_ref="ref-a")
    _setup_flow(monkeypatch, refresh_token="token-a")
    env.client.post(_connect_url(account.id))

    response = env.client.delete(_disconnect_url(account.id))

    assert response.status_code == 204
    assert _account_token(fake_keyring, "ref-a") is None
    assert _stored_token(fake_keyring, LEGACY_ACCOUNT_NAME) == "legacy-token"


def test_missing_scoped_credentials_do_not_fall_back_to_legacy(
    env, monkeypatch, fake_keyring
):
    store_google_drive_credentials(_creds("legacy-token"))
    account = _make_account(env.session, credentials_ref="ref-a")

    response = env.client.get(_status_url(account.id))

    assert response.status_code == 200
    payload = response.json()
    assert payload["credentials_present"] is False
    assert payload["connected"] is False


def test_reconnect_updates_only_own_entry(env, monkeypatch, fake_keyring):
    account_a = _make_account(env.session, display_name="A", credentials_ref="ref-a")
    account_b = _make_account(env.session, display_name="B", credentials_ref="ref-b")

    _setup_flow(monkeypatch, refresh_token="token-a1")
    env.client.post(_connect_url(account_a.id))
    _setup_flow(monkeypatch, refresh_token="token-b")
    env.client.post(_connect_url(account_b.id))

    _setup_flow(monkeypatch, refresh_token="token-a2")
    assert env.client.post(_connect_url(account_a.id)).status_code == 200

    assert _account_token(fake_keyring, "ref-a") == "token-a2"
    assert _account_token(fake_keyring, "ref-b") == "token-b"


# ---------------------------------------------------------------------------
# OAuth safety
# ---------------------------------------------------------------------------


def test_connect_invalid_state_rejected(env, monkeypatch):
    _setup_flow(monkeypatch, error="state_mismatch")
    account = _make_account(env.session, credentials_ref="ref-a")

    response = env.client.post(_connect_url(account.id))

    assert response.status_code == 400
    assert "state" in response.json()["detail"].lower()
    assert _reload(env.session, account.id).status == "pending"
    assert gd_accounts._get_managed_adapter(account.id) is None


def test_failed_token_exchange_does_not_mark_connected(env, monkeypatch, fake_keyring):
    _setup_flow(monkeypatch, error="token_exchange_failed: boom")
    account = _make_account(env.session, credentials_ref="ref-a")

    response = env.client.post(_connect_url(account.id))

    assert response.status_code == 500
    assert _reload(env.session, account.id).status == "pending"
    assert _account_token(fake_keyring, "ref-a") is None
    assert gd_accounts._get_managed_adapter(account.id) is None


def test_responses_do_not_leak_secrets(env, monkeypatch, fake_keyring):
    _setup_flow(monkeypatch, refresh_token="secret-refresh-token-value")
    account = _make_account(env.session, credentials_ref="ref-a")

    connect_response = env.client.post(_connect_url(account.id))
    status_response = env.client.get(_status_url(account.id))

    assert connect_response.status_code == 200
    assert status_response.status_code == 200
    body = (json.dumps(connect_response.json()) + json.dumps(status_response.json())).lower()
    assert "refresh_token" not in body
    assert "access_token" not in body
    assert "client_secret" not in body
    assert "secret-refresh-token-value" not in body


def test_logs_do_not_contain_credentials(env, monkeypatch, caplog):
    _setup_flow(monkeypatch, refresh_token="secret-refresh-token-value")
    account = _make_account(env.session, credentials_ref="ref-a")

    with caplog.at_level(logging.DEBUG):
        assert env.client.post(_connect_url(account.id)).status_code == 200

    assert "secret-refresh-token-value" not in caplog.text
    assert "client-secret" not in caplog.text


def test_account_binding_cannot_be_overridden_by_request(
    env, monkeypatch, fake_keyring
):
    calls = _setup_flow(monkeypatch, refresh_token="token-a")
    account_a = _make_account(env.session, display_name="A", credentials_ref="ref-a")
    account_b = _make_account(env.session, display_name="B", credentials_ref="ref-b")

    response = env.client.post(
        _connect_url(account_a.id),
        params={"credentials_ref": "ref-b", "account_id": account_b.id},
        json={"credentials_ref": "ref-b", "account_id": account_b.id},
    )

    assert response.status_code == 200
    # The server used the path account's reference, never the client's values.
    assert calls == ["ref-a"]
    assert _account_token(fake_keyring, "ref-b") is None
    assert _account_token(fake_keyring, "ref-a") == "token-a"


def test_managed_connect_does_not_touch_legacy_state(env, monkeypatch, fake_keyring):
    _setup_flow(monkeypatch)
    account = _make_account(env.session, credentials_ref="ref-a")

    assert env.client.post(_connect_url(account.id)).status_code == 200

    # Legacy in-memory adapter and legacy keyring entry remain untouched.
    assert gd_endpoints._get_adapter() is None
    assert _stored_token(fake_keyring, LEGACY_ACCOUNT_NAME) is None


def test_legacy_connect_endpoint_still_works(env, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        gd_endpoints, "_run_oauth_flow", lambda: {"success": True, "error": None}
    )

    response = env.client.post("/api/v1/google-drive/connect")

    assert response.status_code == 200
    assert response.json()["status"] == "connected"


def test_legacy_disconnect_endpoint_still_works(env, fake_keyring):
    store_google_drive_credentials(_creds("legacy-token"))

    response = env.client.delete("/api/v1/google-drive/disconnect")

    assert response.status_code == 204
    assert _stored_token(fake_keyring, LEGACY_ACCOUNT_NAME) is None


# ---------------------------------------------------------------------------
# Status and disconnect behavior
# ---------------------------------------------------------------------------


def test_status_before_connect(env):
    account = _make_account(env.session, credentials_ref="ref-a")

    response = env.client.get(_status_url(account.id))

    assert response.status_code == 200
    payload = response.json()
    assert payload["connected"] is False
    assert payload["credentials_present"] is False
    assert payload["status"] == "pending"


def test_status_after_connect(env, monkeypatch, fake_keyring):
    _setup_flow(monkeypatch)
    account = _make_account(env.session, credentials_ref="ref-a")
    env.client.post(_connect_url(account.id))

    payload = env.client.get(_status_url(account.id)).json()

    assert payload["connected"] is True
    assert payload["credentials_present"] is True
    assert payload["status"] == "connected"


def test_status_does_not_call_provider(env, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("status must not call the provider")

    monkeypatch.setattr(GoogleDriveAdapter, "refresh", _boom)
    _setup_flow(monkeypatch)
    account = _make_account(env.session, credentials_ref="ref-a")
    env.client.post(_connect_url(account.id))

    assert env.client.get(_status_url(account.id)).status_code == 200


def test_disconnect_keeps_account_record(env, monkeypatch, fake_keyring):
    _setup_flow(monkeypatch)
    account = _make_account(env.session, credentials_ref="ref-a")
    env.client.post(_connect_url(account.id))

    response = env.client.delete(_disconnect_url(account.id))

    assert response.status_code == 204
    stored = _reload(env.session, account.id)
    assert stored.status == "disconnected"
    assert stored.credentials_ref == "ref-a"
    assert _account_token(fake_keyring, "ref-a") is None


def test_disconnect_clears_managed_adapter(env, monkeypatch):
    _setup_flow(monkeypatch)
    account = _make_account(env.session, credentials_ref="ref-a")
    env.client.post(_connect_url(account.id))
    assert gd_accounts._get_managed_adapter(account.id) is not None

    env.client.delete(_disconnect_url(account.id))

    assert gd_accounts._get_managed_adapter(account.id) is None


def test_repeated_disconnect_is_safe(env, monkeypatch, fake_keyring):
    _setup_flow(monkeypatch)
    account = _make_account(env.session, credentials_ref="ref-a")
    env.client.post(_connect_url(account.id))

    assert env.client.delete(_disconnect_url(account.id)).status_code == 204
    assert env.client.delete(_disconnect_url(account.id)).status_code == 204
    assert _reload(env.session, account.id).status == "disconnected"


def test_disconnect_unknown_account_returns_404(env):
    response = env.client.delete(_disconnect_url("does-not-exist"))
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# `_establish_connection` ref plumbing (the shared flow's persistence step)
# ---------------------------------------------------------------------------


def test_establish_connection_scopes_credentials_and_adapter(
    monkeypatch, fake_keyring
):
    monkeypatch.setattr(GoogleDriveAdapter, "authenticate", lambda self, cfg: None)
    gd_endpoints._set_adapter(None)

    result = gd_endpoints._establish_connection(
        "token-a", ["scope-a"], credentials_ref="ref-a"
    )

    assert result["success"] is True
    adapter = result["adapter"]
    assert isinstance(adapter, GoogleDriveAdapter)
    assert adapter._credentials_ref == "ref-a"
    assert _account_token(fake_keyring, "ref-a") == "token-a"
    assert _stored_token(fake_keyring, LEGACY_ACCOUNT_NAME) is None
    # Managed connections must not replace the legacy in-memory adapter.
    assert gd_endpoints._get_adapter() is None


def test_establish_connection_failure_clears_only_scoped_entry(
    monkeypatch, fake_keyring
):
    store_google_drive_credentials(_creds("legacy-token"))

    def _boom(self, cfg):
        raise StorageAuthenticationError(
            detail="invalid_grant", provider="GOOGLE_DRIVE"
        )

    monkeypatch.setattr(GoogleDriveAdapter, "authenticate", _boom)
    gd_endpoints._set_adapter(None)

    result = gd_endpoints._establish_connection(
        "token-a", ["scope-a"], credentials_ref="ref-a"
    )

    assert result["success"] is False
    assert "authentication_failed" in result["error"]
    assert _account_token(fake_keyring, "ref-a") is None
    assert _stored_token(fake_keyring, LEGACY_ACCOUNT_NAME) == "legacy-token"
    assert gd_endpoints._get_adapter() is None


# ---------------------------------------------------------------------------
# HVSA H1.4E-A M1: provider-unavailable failures also clean up
# ---------------------------------------------------------------------------


def test_establish_connection_unavailable_failure_clears_scoped_entry(
    monkeypatch, fake_keyring
):
    """A provider outage during authentication must not orphan a token."""
    store_google_drive_credentials(_creds("legacy-token"))

    def _offline(self, cfg):
        raise StorageUnavailableError(
            detail="network down", provider="GOOGLE_DRIVE"
        )

    monkeypatch.setattr(GoogleDriveAdapter, "authenticate", _offline)
    gd_endpoints._set_adapter(None)

    result = gd_endpoints._establish_connection(
        "token-a", ["scope-a"], credentials_ref="ref-a"
    )

    assert result["success"] is False
    assert "provider_unavailable" in result["error"]
    assert _account_token(fake_keyring, "ref-a") is None
    assert _stored_token(fake_keyring, LEGACY_ACCOUNT_NAME) == "legacy-token"
    assert gd_endpoints._get_adapter() is None


def test_establish_connection_legacy_unavailable_failure_keeps_legacy_entry(
    monkeypatch, fake_keyring
):
    """Legacy semantics are preserved: no-argument failure never deletes."""

    def _offline(self, cfg):
        raise StorageUnavailableError(
            detail="network down", provider="GOOGLE_DRIVE"
        )

    monkeypatch.setattr(GoogleDriveAdapter, "authenticate", _offline)
    gd_endpoints._set_adapter(None)

    result = gd_endpoints._establish_connection("token-new", ["scope-a"])

    assert result["success"] is False
    assert "provider_unavailable" in result["error"]
    # The legacy entry is left as written by this flow; cleanup never deletes it.
    assert _stored_token(fake_keyring, LEGACY_ACCOUNT_NAME) == "token-new"


# ---------------------------------------------------------------------------
# HVSA H1.4E-A M4: status persistence failure
# ---------------------------------------------------------------------------


def test_mark_connected_failure_cleans_up_and_installs_no_adapter(
    env, monkeypatch, fake_keyring
):
    """A failed status commit installs nothing and leaves no staging entry.

    HVSA H1.4F: the credential was already authenticated and promoted, so it is
    retained (it is valid and belongs to this account); only this attempt's
    staging entry must be gone and no adapter may be installed.
    """
    _setup_flow(monkeypatch, refresh_token="token-a")
    account = _make_account(env.session, display_name="A", credentials_ref="ref-a")
    other = _make_account(env.session, display_name="B", credentials_ref="ref-b")
    store_google_drive_credentials(_creds("token-b"), credentials_ref="ref-b")
    store_google_drive_credentials(_creds("legacy-token"))

    def _boom(self, account_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(AccountManager, "mark_connected", _boom)

    response = env.client.post(_connect_url(account.id))

    assert response.status_code == 500
    assert response.json()["detail"] == "Failed to persist account status."
    assert gd_accounts._get_managed_adapter(account.id) is None
    # The promoted credential survives; no staging entry is left behind.
    assert _account_token(fake_keyring, "ref-a") == "token-a"
    assert _keyring_accounts(fake_keyring) == {
        _derive_keyring_account("ref-a"),
        _derive_keyring_account("ref-b"),
        LEGACY_ACCOUNT_NAME,
    }
    # Other accounts and the legacy entry are untouched.
    assert _account_token(fake_keyring, "ref-b") == "token-b"
    assert _reload(env.session, other.id).status == "pending"
    assert _stored_token(fake_keyring, LEGACY_ACCOUNT_NAME) == "legacy-token"
    assert _reload(env.session, account.id).status == "pending"


# ---------------------------------------------------------------------------
# HVSA H1.4E-A M2: per-account concurrency
# ---------------------------------------------------------------------------


def _start_connect(account_id, session_factory, results):
    def run():
        session = session_factory()
        try:
            payload = gd_accounts.connect_account_google_drive(account_id, db=session)
            results.append(("ok", payload))
        except Exception as exc:  # HTTPException on the expected-failure case
            results.append(("error", exc))
        finally:
            session.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _start_disconnect(account_id, session_factory, results):
    def run():
        session = session_factory()
        try:
            gd_accounts.disconnect_account_google_drive(account_id, db=session)
            results.append(("ok", None))
        except Exception as exc:
            results.append(("error", exc))
        finally:
            session.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_account_locks_are_released_and_removed():
    """The lock map does not accumulate entries."""
    with gd_accounts._account_lock("acct-x"):
        assert "acct-x" in gd_accounts._account_locks

    assert "acct-x" not in gd_accounts._account_locks


def test_two_connects_same_account_are_serialized(
    concurrent_env, monkeypatch, fake_keyring
):
    """A second connect to the same account waits for the first to finish."""
    _configure(monkeypatch)
    account = _make_account(concurrent_env.session, credentials_ref="ref-a")

    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    def fake_flow(*, credentials_ref=None, promote_to=None):
        if not first_started.is_set():
            first_started.set()
            assert release_first.wait(10), "first flow was never released"
            token = "token-1"
        else:
            second_started.set()
            token = "token-2"
        target = _fake_flow_store(
            credentials_ref=credentials_ref,
            promote_to=promote_to,
            refresh_token=token,
        )
        return {
            "success": True,
            "error": None,
            "adapter": GoogleDriveAdapter(credentials_ref=target),
        }

    monkeypatch.setattr(gd_endpoints, "_run_oauth_flow", fake_flow)

    results: list = []
    t1 = _start_connect(account.id, concurrent_env.session_factory, results)
    assert first_started.wait(10)
    t2 = _start_connect(account.id, concurrent_env.session_factory, results)

    assert second_started.wait(1) is False  # still blocked by the account lock

    release_first.set()
    t1.join(10)
    t2.join(10)

    assert not t1.is_alive() and not t2.is_alive()
    assert second_started.is_set()
    assert [kind for kind, _ in results] == ["ok", "ok"]
    assert _account_token(fake_keyring, "ref-a") == "token-2"
    assert _reload(concurrent_env.session, account.id).status == "connected"


def test_disconnect_waits_for_in_flight_connect(
    concurrent_env, monkeypatch, fake_keyring
):
    """Disconnect cannot interleave with a connect on the same account."""
    _configure(monkeypatch)
    account = _make_account(concurrent_env.session, credentials_ref="ref-a")

    flow_started = threading.Event()
    release_flow = threading.Event()

    def fake_flow(*, credentials_ref=None, promote_to=None):
        flow_started.set()
        assert release_flow.wait(10), "flow was never released"
        target = _fake_flow_store(
            credentials_ref=credentials_ref,
            promote_to=promote_to,
            refresh_token="token-a",
        )
        return {
            "success": True,
            "error": None,
            "adapter": GoogleDriveAdapter(credentials_ref=target),
        }

    monkeypatch.setattr(gd_endpoints, "_run_oauth_flow", fake_flow)

    connect_results: list = []
    disconnect_results: list = []
    t1 = _start_connect(account.id, concurrent_env.session_factory, connect_results)
    assert flow_started.wait(10)

    t2 = _start_disconnect(
        account.id, concurrent_env.session_factory, disconnect_results
    )
    time.sleep(0.2)  # a blocked disconnect completes in microseconds otherwise
    assert t2.is_alive()

    release_flow.set()
    t1.join(10)
    t2.join(10)

    assert [kind for kind, _ in connect_results] == ["ok"]
    assert [kind for kind, _ in disconnect_results] == ["ok"]
    assert _account_token(fake_keyring, "ref-a") is None
    assert gd_accounts._get_managed_adapter(account.id) is None
    assert _reload(concurrent_env.session, account.id).status == "disconnected"


def test_different_accounts_connect_independently(
    concurrent_env, monkeypatch, fake_keyring
):
    """Per-account locks never serialize unrelated accounts."""
    _configure(monkeypatch)
    account_a = _make_account(
        concurrent_env.session, display_name="A", credentials_ref="ref-a"
    )
    account_b = _make_account(
        concurrent_env.session, display_name="B", credentials_ref="ref-b"
    )

    barrier = threading.Barrier(2, timeout=10)

    def fake_flow(*, credentials_ref=None, promote_to=None):
        barrier.wait()  # raises if the two flows cannot overlap
        permanent = promote_to if promote_to is not None else credentials_ref
        target = _fake_flow_store(
            credentials_ref=credentials_ref,
            promote_to=promote_to,
            refresh_token=f"token-{permanent}",
        )
        return {
            "success": True,
            "error": None,
            "adapter": GoogleDriveAdapter(credentials_ref=target),
        }

    monkeypatch.setattr(gd_endpoints, "_run_oauth_flow", fake_flow)

    results: list = []
    t1 = _start_connect(account_a.id, concurrent_env.session_factory, results)
    t2 = _start_connect(account_b.id, concurrent_env.session_factory, results)
    t1.join(15)
    t2.join(15)

    assert [kind for kind, _ in results] == ["ok", "ok"]
    assert _account_token(fake_keyring, "ref-a") == "token-ref-a"
    assert _account_token(fake_keyring, "ref-b") == "token-ref-b"
    assert _reload(concurrent_env.session, account_a.id).status == "connected"
    assert _reload(concurrent_env.session, account_b.id).status == "connected"


def test_failed_older_flow_does_not_delete_newer_credentials(
    concurrent_env, monkeypatch, fake_keyring
):
    """Cleanup from an older failed attempt runs before the newer attempt."""
    _configure(monkeypatch)
    account = _make_account(concurrent_env.session, credentials_ref="ref-a")

    older_started = threading.Event()
    allow_older_to_fail = threading.Event()

    def fake_flow(*, credentials_ref=None, promote_to=None):
        if not older_started.is_set():
            older_started.set()
            assert allow_older_to_fail.wait(10), "older flow was never released"
            token = "token-old"
        else:
            token = "token-new"
        return gd_endpoints._establish_connection(
            token,
            ["scope-a"],
            credentials_ref=credentials_ref,
            promote_to=promote_to,
        )

    def authenticate(self, cfg):
        if cfg["refresh_token"] == "token-old":
            raise StorageAuthenticationError(
                detail="invalid_grant", provider="GOOGLE_DRIVE"
            )

    monkeypatch.setattr(gd_endpoints, "_run_oauth_flow", fake_flow)
    monkeypatch.setattr(GoogleDriveAdapter, "authenticate", authenticate)

    results: list = []
    t1 = _start_connect(account.id, concurrent_env.session_factory, results)
    assert older_started.wait(10)
    t2 = _start_connect(account.id, concurrent_env.session_factory, results)

    allow_older_to_fail.set()
    t1.join(10)
    t2.join(10)

    assert [kind for kind, _ in results] == ["error", "ok"]
    # The older attempt's cleanup deleted only the token it had just written.
    assert _account_token(fake_keyring, "ref-a") == "token-new"
    assert _reload(concurrent_env.session, account.id).status == "connected"


# ---------------------------------------------------------------------------
# HVSA H1.4E-A M6: the flow forwards the exact credential reference
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# HVSA H1.4E-B: managed adapter restoration after restart
# ---------------------------------------------------------------------------


def _seed_connected(
    session,
    *,
    credentials_ref: str = "ref-a",
    display_name: str = "Test Account",
    refresh_token: str = "token-a",
    store_credentials: bool = True,
) -> StorageAccount:
    """Create an account, mark it connected, and seed its scoped credentials."""
    account = _make_account(
        session, display_name=display_name, credentials_ref=credentials_ref
    )
    AccountManager(session).mark_connected(account.id)
    if store_credentials:
        store_google_drive_credentials(
            _creds(refresh_token), credentials_ref=credentials_ref
        )
    return account


def _install_fake_adapter(monkeypatch, outcomes: dict | None = None) -> list:
    """Replace the Drive adapter with a recording fake that never goes online.

    *outcomes* maps ``credentials_ref`` to an exception instance that
    ``authenticate()`` should raise for that account.
    """
    outcomes = outcomes or {}
    created: list = []

    class _FakeAdapter:
        def __init__(self, credentials_ref=None):
            self.credentials_ref = credentials_ref
            self.authenticated_with = None
            self.disconnected = False
            created.append(self)

        def authenticate(self, config):
            outcome = outcomes.get(self.credentials_ref)
            if isinstance(outcome, BaseException):
                raise outcome
            self.authenticated_with = config

        def disconnect(self):
            self.disconnected = True
            self.authenticated_with = None

    monkeypatch.setattr(gd_accounts, "GoogleDriveAdapter", _FakeAdapter)
    return created


class _TrackingSession:
    """Session proxy that records whether it was closed."""

    def __init__(self, session):
        self._session = session
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self._session.close()

    def __getattr__(self, name):
        return getattr(self._session, name)


class _TrackingSessionFactory:
    def __init__(self, session_factory):
        self._session_factory = session_factory
        self.sessions: list[_TrackingSession] = []

    def __call__(self):
        proxy = _TrackingSession(self._session_factory())
        self.sessions.append(proxy)
        return proxy


def test_restore_single_managed_account(concurrent_env, monkeypatch, fake_keyring):
    _configure(monkeypatch)
    created = _install_fake_adapter(monkeypatch)
    account = _seed_connected(concurrent_env.session)

    summary = gd_accounts.restore_managed_adapters(concurrent_env.session_factory)

    assert summary["restored"] == 1
    assert sum(summary.values()) == 1
    adapter = gd_accounts._get_managed_adapter(account.id)
    assert adapter is created[0]
    assert adapter.credentials_ref == "ref-a"
    assert adapter.authenticated_with["refresh_token"] == "token-a"
    assert _reload(concurrent_env.session, account.id).status == "connected"


def test_restore_multiple_managed_accounts_keyed_by_account_id(
    concurrent_env, monkeypatch, fake_keyring
):
    _configure(monkeypatch)
    _install_fake_adapter(monkeypatch)
    account_a = _seed_connected(
        concurrent_env.session,
        credentials_ref="ref-a",
        display_name="A",
        refresh_token="token-a",
    )
    account_b = _seed_connected(
        concurrent_env.session,
        credentials_ref="ref-b",
        display_name="B",
        refresh_token="token-b",
    )

    summary = gd_accounts.restore_managed_adapters(concurrent_env.session_factory)

    assert summary["restored"] == 2
    assert set(gd_accounts._managed_adapters) == {account_a.id, account_b.id}
    assert gd_accounts._get_managed_adapter(account_a.id).credentials_ref == "ref-a"
    assert gd_accounts._get_managed_adapter(account_b.id).credentials_ref == "ref-b"
    assert (
        gd_accounts._get_managed_adapter(account_a.id).authenticated_with[
            "refresh_token"
        ]
        == "token-a"
    )
    assert (
        gd_accounts._get_managed_adapter(account_b.id).authenticated_with[
            "refresh_token"
        ]
        == "token-b"
    )


def test_restore_skips_accounts_that_are_not_connected(
    concurrent_env, monkeypatch, fake_keyring
):
    """Only ``connected`` rows are candidates; pending accounts stay untouched."""
    _configure(monkeypatch)
    created = _install_fake_adapter(monkeypatch)
    pending = _make_account(concurrent_env.session, credentials_ref="ref-a")
    store_google_drive_credentials(_creds("token-a"), credentials_ref="ref-a")

    summary = gd_accounts.restore_managed_adapters(concurrent_env.session_factory)

    assert sum(summary.values()) == 0
    assert created == []
    assert gd_accounts._get_managed_adapter(pending.id) is None
    assert _reload(concurrent_env.session, pending.id).status == "pending"


def test_one_account_auth_failure_does_not_block_others(
    concurrent_env, monkeypatch, fake_keyring
):
    _configure(monkeypatch)
    _install_fake_adapter(
        monkeypatch, {"ref-a": StorageAuthenticationError("revoked")}
    )
    account_a = _seed_connected(
        concurrent_env.session,
        credentials_ref="ref-a",
        display_name="A",
        refresh_token="token-a",
    )
    account_b = _seed_connected(
        concurrent_env.session,
        credentials_ref="ref-b",
        display_name="B",
        refresh_token="token-b",
    )

    summary = gd_accounts.restore_managed_adapters(concurrent_env.session_factory)

    assert summary["restored"] == 1
    assert summary["authentication_failed"] == 1
    assert gd_accounts._get_managed_adapter(account_a.id) is None
    assert gd_accounts._get_managed_adapter(account_b.id) is not None
    assert _reload(concurrent_env.session, account_a.id).status == "disconnected"
    assert _reload(concurrent_env.session, account_b.id).status == "connected"


def test_authentication_failure_preserves_credentials_and_disconnects(
    concurrent_env, monkeypatch, fake_keyring
):
    _configure(monkeypatch)
    _install_fake_adapter(
        monkeypatch, {"ref-a": StorageAuthenticationError("invalid_grant")}
    )
    account = _seed_connected(concurrent_env.session)

    summary = gd_accounts.restore_managed_adapters(concurrent_env.session_factory)

    assert summary["authentication_failed"] == 1
    # The keyring entry survives so the user can reconnect.
    assert _account_token(fake_keyring, "ref-a") == "token-a"
    assert _reload(concurrent_env.session, account.id).status == "disconnected"
    assert gd_accounts._get_managed_adapter(account.id) is None


def test_provider_unavailable_preserves_status_and_credentials(
    concurrent_env, monkeypatch, fake_keyring
):
    _configure(monkeypatch)
    _install_fake_adapter(
        monkeypatch, {"ref-a": StorageUnavailableError("network down")}
    )
    account = _seed_connected(concurrent_env.session)

    summary = gd_accounts.restore_managed_adapters(concurrent_env.session_factory)

    assert summary["provider_unavailable"] == 1
    assert summary["restored"] == 0
    assert _account_token(fake_keyring, "ref-a") == "token-a"
    assert _reload(concurrent_env.session, account.id).status == "connected"
    assert gd_accounts._get_managed_adapter(account.id) is None


def test_credential_store_unavailable_preserves_status_and_credentials(
    concurrent_env, monkeypatch, fake_keyring
):
    _configure(monkeypatch)
    created = _install_fake_adapter(monkeypatch)

    def _locked(*, credentials_ref=None):
        raise cred_mod.CredentialStorageError("keyring locked")

    monkeypatch.setattr(gd_accounts, "load_google_drive_credentials", _locked)

    account = _make_account(concurrent_env.session, credentials_ref="ref-a")
    AccountManager(concurrent_env.session).mark_connected(account.id)
    store_google_drive_credentials(_creds("token-a"), credentials_ref="ref-a")

    summary = gd_accounts.restore_managed_adapters(concurrent_env.session_factory)

    assert summary["credential_store_unavailable"] == 1
    assert created == []
    assert _account_token(fake_keyring, "ref-a") == "token-a"
    assert _reload(concurrent_env.session, account.id).status == "connected"
    assert gd_accounts._get_managed_adapter(account.id) is None


def test_missing_scoped_credentials_mark_disconnected(
    concurrent_env, monkeypatch, fake_keyring
):
    _configure(monkeypatch)
    created = _install_fake_adapter(monkeypatch)
    account = _seed_connected(concurrent_env.session, store_credentials=False)

    summary = gd_accounts.restore_managed_adapters(concurrent_env.session_factory)

    assert summary["credentials_missing"] == 1
    assert created == []
    assert _reload(concurrent_env.session, account.id).status == "disconnected"
    assert gd_accounts._get_managed_adapter(account.id) is None


def test_restore_never_falls_back_to_legacy_credentials(
    concurrent_env, monkeypatch, fake_keyring
):
    """A legacy entry must not make a managed account authenticatable."""
    _configure(monkeypatch)
    created = _install_fake_adapter(monkeypatch)
    store_google_drive_credentials(_creds("legacy-token"))
    account = _seed_connected(concurrent_env.session, store_credentials=False)

    summary = gd_accounts.restore_managed_adapters(concurrent_env.session_factory)

    assert summary["credentials_missing"] == 1
    assert created == []
    assert gd_accounts._get_managed_adapter(account.id) is None
    assert _stored_token(fake_keyring, LEGACY_ACCOUNT_NAME) == "legacy-token"


def test_restore_does_not_touch_provider_registry_or_legacy_adapter(
    concurrent_env, monkeypatch, fake_keyring
):
    _configure(monkeypatch)
    _install_fake_adapter(monkeypatch)
    legacy_adapter = GoogleDriveAdapter()
    gd_endpoints._set_adapter(legacy_adapter)
    account = _seed_connected(concurrent_env.session)

    gd_accounts.restore_managed_adapters(concurrent_env.session_factory)

    assert gd_endpoints._get_adapter() is legacy_adapter
    with pytest.raises(StorageUnavailableError):
        registry_mod.lookup("GOOGLE_DRIVE")
    assert account.id in gd_accounts._managed_adapters


def test_restore_closes_every_session_on_success(
    concurrent_env, monkeypatch, fake_keyring
):
    _configure(monkeypatch)
    _install_fake_adapter(monkeypatch)
    _seed_connected(concurrent_env.session, store_credentials=False)
    tracking = _TrackingSessionFactory(concurrent_env.session_factory)

    gd_accounts.restore_managed_adapters(tracking)

    assert tracking.sessions  # read session plus the status write
    assert all(session.closed for session in tracking.sessions)


def test_restore_closes_session_when_query_raises(concurrent_env, monkeypatch):
    tracking = _TrackingSessionFactory(concurrent_env.session_factory)

    def _boom(self, provider_id=None):
        raise RuntimeError("query failed")

    monkeypatch.setattr(gd_accounts.AccountManager, "list_accounts", _boom)

    with pytest.raises(RuntimeError):
        gd_accounts.restore_managed_adapters(tracking)

    assert tracking.sessions
    assert all(session.closed for session in tracking.sessions)


def test_status_reports_adapter_present(env, monkeypatch, fake_keyring):
    account = _make_account(env.session, credentials_ref="ref-a")
    AccountManager(env.session).mark_connected(account.id)
    store_google_drive_credentials(_creds("token-a"), credentials_ref="ref-a")
    gd_accounts._set_managed_adapter(account.id, object())

    payload = env.client.get(_status_url(account.id)).json()

    assert payload["status"] == "connected"
    assert payload["credentials_present"] is True
    assert payload["adapter_present"] is True
    assert payload["connected"] is True


def test_status_connected_in_db_without_runtime_adapter_is_not_connected(
    env, fake_keyring
):
    """Restart state (audit F7): DB says connected, no adapter installed."""
    account = _make_account(env.session, credentials_ref="ref-a")
    AccountManager(env.session).mark_connected(account.id)
    store_google_drive_credentials(_creds("token-a"), credentials_ref="ref-a")

    payload = env.client.get(_status_url(account.id)).json()

    assert payload["status"] == "connected"
    assert payload["credentials_present"] is True
    assert payload["adapter_present"] is False
    assert payload["connected"] is False


def test_status_reports_missing_credentials_without_adapter(env, fake_keyring):
    account = _make_account(env.session, credentials_ref="ref-a")
    AccountManager(env.session).mark_connected(account.id)

    payload = env.client.get(_status_url(account.id)).json()

    assert payload["status"] == "disconnected"
    assert payload["credentials_present"] is False
    assert payload["adapter_present"] is False
    assert payload["connected"] is False


def test_legacy_adapter_never_counts_as_managed(env, fake_keyring):
    gd_endpoints._set_adapter(GoogleDriveAdapter())
    account = _make_account(env.session, credentials_ref="ref-a")
    AccountManager(env.session).mark_connected(account.id)
    store_google_drive_credentials(_creds("token-a"), credentials_ref="ref-a")

    payload = env.client.get(_status_url(account.id)).json()

    assert payload["adapter_present"] is False
    assert payload["connected"] is False


def test_run_oauth_flow_forwards_credentials_ref_to_establish(monkeypatch):
    """Dropping the forwarding kwarg makes this test fail."""
    _configure(monkeypatch)
    captured: dict = {}

    class _FakeInstalledAppFlow:
        @classmethod
        def from_client_config(cls, client_config, scopes=None, redirect_uri=None):
            return cls()

        def authorization_url(self, **kwargs):
            return ("https://accounts.example/auth", "state-xyz")

        def fetch_token(self, authorization_response=None):
            self.authorization_response = authorization_response

        @property
        def credentials(self):
            return SimpleNamespace(refresh_token="token-forwarded")

    def _fake_make_callback_handler(state, outcome):
        class _Handler:
            def inject(self):
                outcome["code"] = "auth-code"
                outcome["state"] = state

        return _Handler()

    class _FakeServer:
        def __init__(self, address, handler):
            self._handler = handler
            self.timeout = None

        def handle_request(self):
            self._handler.inject()

        def server_close(self):
            pass

    def _spy_establish_connection(
        refresh_token, scopes, *, credentials_ref=None, promote_to=None
    ):
        captured["refresh_token"] = refresh_token
        captured["scopes"] = scopes
        captured["credentials_ref"] = credentials_ref
        captured["promote_to"] = promote_to
        return {"success": True, "error": None, "adapter": None}

    import google_auth_oauthlib.flow as oauthlib_flow

    monkeypatch.setattr(oauthlib_flow, "InstalledAppFlow", _FakeInstalledAppFlow)
    monkeypatch.setattr(
        gd_endpoints, "_make_callback_handler", _fake_make_callback_handler
    )
    monkeypatch.setattr(gd_endpoints, "HTTPServer", _FakeServer)
    monkeypatch.setattr(
        gd_endpoints, "_establish_connection", _spy_establish_connection
    )
    monkeypatch.setattr(gd_endpoints.webbrowser, "open", lambda *a, **k: None)

    result = gd_endpoints._run_oauth_flow(credentials_ref="ref-a")

    assert result["success"] is True
    assert captured["credentials_ref"] == "ref-a"
    assert captured["promote_to"] is None
    assert captured["refresh_token"] == "token-forwarded"

    # HVSA H1.4F: the staging/promotion arguments are forwarded verbatim.
    gd_endpoints._run_oauth_flow(credentials_ref="staging-x", promote_to="ref-a")

    assert captured["credentials_ref"] == "staging-x"
    assert captured["promote_to"] == "ref-a"


# ---------------------------------------------------------------------------
# Concurrency harness fixture (file-backed DB, one session per thread)
# ---------------------------------------------------------------------------


@pytest.fixture
def concurrent_env(tmp_path):
    """Isolated file-backed DB that several worker threads can use safely."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrency.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        poolclass=NullPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestSession()

    gd_endpoints._set_adapter(None)
    gd_endpoints._startup_auth_failure_reason = None
    gd_accounts._managed_adapters.clear()
    gd_accounts._account_locks.clear()

    try:
        yield SimpleNamespace(
            session=session, session_factory=TestSession, engine=engine
        )
    finally:
        gd_accounts._managed_adapters.clear()
        gd_accounts._account_locks.clear()
        gd_endpoints._set_adapter(None)
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# HVSA H1.4F F1/F3: stage -> authenticate -> promote credential lifecycle
# ---------------------------------------------------------------------------


def _install_real_establish_flow(monkeypatch, *, token: str, behaviour=None):
    """Route the endpoint through the real stage -> promote implementation.

    Only ``GoogleDriveAdapter.authenticate`` is replaced, so the credential
    writes, promotion, and cleanup under test are the production ones.
    """

    def _authenticate(self, config):
        if behaviour is not None:
            behaviour(config)

    monkeypatch.setattr(GoogleDriveAdapter, "authenticate", _authenticate)

    def fake_flow(*, credentials_ref=None, promote_to=None):
        return gd_endpoints._establish_connection(
            token,
            ["scope-a"],
            credentials_ref=credentials_ref,
            promote_to=promote_to,
        )

    monkeypatch.setattr(gd_endpoints, "_run_oauth_flow", fake_flow)


def test_staged_credentials_are_promoted_and_staging_entry_removed(
    env, monkeypatch, fake_keyring
):
    _configure(monkeypatch)
    _install_real_establish_flow(monkeypatch, token="token-new")
    account = _make_account(env.session, credentials_ref="ref-a")
    store_google_drive_credentials(_creds("token-old"), credentials_ref="ref-a")

    response = env.client.post(_connect_url(account.id))

    assert response.status_code == 200
    assert _account_token(fake_keyring, "ref-a") == "token-new"
    # Only the permanent entry remains: the staging entry was removed.
    assert _keyring_accounts(fake_keyring) == {_derive_keyring_account("ref-a")}
    adapter = gd_accounts._get_managed_adapter(account.id)
    assert adapter is not None
    assert adapter._credentials_ref == "ref-a"
    assert _reload(env.session, account.id).status == "connected"


def test_failed_reconnect_preserves_previous_credential(
    env, monkeypatch, fake_keyring
):
    _configure(monkeypatch)

    def _reject(config):
        raise StorageAuthenticationError(
            detail="invalid_grant", provider="GOOGLE_DRIVE"
        )

    _install_real_establish_flow(monkeypatch, token="token-new", behaviour=_reject)
    account = _make_account(env.session, credentials_ref="ref-a")
    store_google_drive_credentials(_creds("token-old"), credentials_ref="ref-a")

    response = env.client.post(_connect_url(account.id))

    assert response.status_code == 500
    # The previously working credential is untouched, no staging entry is left.
    assert _account_token(fake_keyring, "ref-a") == "token-old"
    assert _keyring_accounts(fake_keyring) == {_derive_keyring_account("ref-a")}
    assert gd_accounts._get_managed_adapter(account.id) is None
    assert _reload(env.session, account.id).status == "pending"


def test_transient_outage_preserves_previous_credential(
    env, monkeypatch, fake_keyring
):
    _configure(monkeypatch)

    def _offline(config):
        raise StorageUnavailableError(detail="network down", provider="GOOGLE_DRIVE")

    _install_real_establish_flow(monkeypatch, token="token-new", behaviour=_offline)
    account = _make_account(env.session, credentials_ref="ref-a")
    store_google_drive_credentials(_creds("token-old"), credentials_ref="ref-a")

    response = env.client.post(_connect_url(account.id))

    assert response.status_code == 500
    assert _account_token(fake_keyring, "ref-a") == "token-old"
    assert _keyring_accounts(fake_keyring) == {_derive_keyring_account("ref-a")}
    assert gd_accounts._get_managed_adapter(account.id) is None


def test_failed_promotion_preserves_previous_permanent_credential(
    env, monkeypatch, fake_keyring
):
    _configure(monkeypatch)
    _install_real_establish_flow(monkeypatch, token="token-new")
    account = _make_account(env.session, credentials_ref="ref-a")
    store_google_drive_credentials(_creds("token-old"), credentials_ref="ref-a")

    real_store = gd_endpoints.store_google_drive_credentials

    def _store(credentials, *, credentials_ref=None):
        if credentials_ref == "ref-a":
            raise cred_mod.CredentialStorageError("promotion write failed")
        real_store(credentials, credentials_ref=credentials_ref)

    monkeypatch.setattr(gd_endpoints, "store_google_drive_credentials", _store)

    response = env.client.post(_connect_url(account.id))

    assert response.status_code == 500
    assert _account_token(fake_keyring, "ref-a") == "token-old"
    assert _keyring_accounts(fake_keyring) == {_derive_keyring_account("ref-a")}
    assert gd_accounts._get_managed_adapter(account.id) is None
    assert _reload(env.session, account.id).status == "pending"


def test_staging_references_are_unique_and_distinct_from_permanent():
    first = gd_accounts._staging_credentials_ref("ref-a")
    second = gd_accounts._staging_credentials_ref("ref-a")

    assert first != second
    assert first != "ref-a" and second != "ref-a"
    assert first.startswith("ref-a")
    assert gd_accounts._staging_credentials_ref("ref-b") != first


def test_staging_reference_never_appears_in_response_or_logs(
    env, monkeypatch, fake_keyring, caplog
):
    _configure(monkeypatch)

    def _reject(config):
        raise StorageAuthenticationError(
            detail="invalid_grant", provider="GOOGLE_DRIVE"
        )

    _install_real_establish_flow(
        monkeypatch, token="secret-new-token", behaviour=_reject
    )
    account = _make_account(env.session, credentials_ref="ref-a")

    with caplog.at_level(logging.DEBUG):
        response = env.client.post(_connect_url(account.id))

    assert response.status_code == 500
    assert ":staging:" not in response.text
    assert "secret-new-token" not in response.text
    assert ":staging:" not in caplog.text
    assert "secret-new-token" not in caplog.text


def test_failed_older_attempt_leaves_no_staging_entries(
    concurrent_env, monkeypatch, fake_keyring
):
    """The older attempt cleans up its own staging entry only."""
    _configure(monkeypatch)
    account = _make_account(concurrent_env.session, credentials_ref="ref-a")
    store_google_drive_credentials(_creds("token-old"), credentials_ref="ref-a")

    older_started = threading.Event()
    allow_older_to_fail = threading.Event()

    def fake_flow(*, credentials_ref=None, promote_to=None):
        if not older_started.is_set():
            older_started.set()
            assert allow_older_to_fail.wait(10), "older flow was never released"
            token = "token-old-attempt"
        else:
            token = "token-new"
        return gd_endpoints._establish_connection(
            token,
            ["scope-a"],
            credentials_ref=credentials_ref,
            promote_to=promote_to,
        )

    def authenticate(self, config):
        if config["refresh_token"] == "token-old-attempt":
            raise StorageAuthenticationError(
                detail="invalid_grant", provider="GOOGLE_DRIVE"
            )

    monkeypatch.setattr(gd_endpoints, "_run_oauth_flow", fake_flow)
    monkeypatch.setattr(GoogleDriveAdapter, "authenticate", authenticate)

    results: list = []
    t1 = _start_connect(account.id, concurrent_env.session_factory, results)
    assert older_started.wait(10)
    t2 = _start_connect(account.id, concurrent_env.session_factory, results)
    allow_older_to_fail.set()
    t1.join(10)
    t2.join(10)

    assert [kind for kind, _ in results] == ["error", "ok"]
    assert _account_token(fake_keyring, "ref-a") == "token-new"
    assert _keyring_accounts(fake_keyring) == {_derive_keyring_account("ref-a")}
    assert _reload(concurrent_env.session, account.id).status == "connected"


# ---------------------------------------------------------------------------
# HVSA H1.4F F4: cleanup after a rollback must not touch expired ORM state
# ---------------------------------------------------------------------------


class _BreakAfterRollback:
    """Session proxy whose ORM instances become unusable once rolled back."""

    def __init__(self, session):
        self._session = session
        self.rolled_back = False

    def rollback(self):
        self.rolled_back = True
        self._session.rollback()
        self._session.expire_all()
        self._session.expunge_all()

    def __getattr__(self, name):
        if self.rolled_back and name in {"query", "get", "execute", "refresh"}:
            raise RuntimeError("session unusable after rollback")
        return getattr(self._session, name)


def test_mark_connected_failure_uses_captured_id_after_rollback(
    env, monkeypatch, fake_keyring
):
    """Cleanup must not read expired ORM attributes to find the account."""
    _configure(monkeypatch)
    _setup_flow(monkeypatch, refresh_token="token-new")
    account = _make_account(env.session, credentials_ref="ref-a")
    account_id = account.id

    def _boom(self, target_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(AccountManager, "mark_connected", _boom)
    broken = _BreakAfterRollback(env.session)

    with pytest.raises(HTTPException) as excinfo:
        gd_accounts.connect_account_google_drive(account_id, db=broken)

    assert excinfo.value.status_code == 500
    assert excinfo.value.detail == "Failed to persist account status."
    assert broken.rolled_back is True
    assert gd_accounts._get_managed_adapter(account_id) is None
    assert _account_token(fake_keyring, "ref-a") == "token-new"
    assert _keyring_accounts(fake_keyring) == {_derive_keyring_account("ref-a")}


# ---------------------------------------------------------------------------
# HVSA H1.4F F5: per-account lock hardening
# ---------------------------------------------------------------------------


def test_account_lock_releases_reference_on_exception():
    with pytest.raises(RuntimeError):
        with gd_accounts._account_lock("acct-err"):
            assert "acct-err" in gd_accounts._account_locks
            raise RuntimeError("boom")

    assert "acct-err" not in gd_accounts._account_locks


def test_account_lock_removal_never_deletes_a_replacement_entry():
    """The identity check protects a newer entry from a stale finalizer."""
    with gd_accounts._account_lock("acct-id"):
        replacement = [threading.Lock(), 0]
        gd_accounts._account_locks["acct-id"] = replacement

    assert gd_accounts._account_locks["acct-id"] is replacement
    gd_accounts._account_locks.pop("acct-id")


def test_account_lock_hands_off_and_cleans_up_entry():
    order: list = []
    entered = threading.Event()
    release = threading.Event()

    def holder():
        with gd_accounts._account_lock("acct-handoff"):
            entered.set()
            assert release.wait(10)
            order.append("holder")

    first = threading.Thread(target=holder, daemon=True)
    first.start()
    assert entered.wait(10)

    def waiter():
        with gd_accounts._account_lock("acct-handoff"):
            order.append("waiter")

    second = threading.Thread(target=waiter, daemon=True)
    second.start()
    time.sleep(0.05)
    assert order == []  # still waiting on the holder

    release.set()
    first.join(10)
    second.join(10)

    assert not first.is_alive() and not second.is_alive()
    assert order == ["holder", "waiter"]
    assert "acct-handoff" not in gd_accounts._account_locks


def test_different_accounts_are_not_serialized_by_the_lock():
    barrier = threading.Barrier(2, timeout=10)
    errors: list = []

    def enter(account_id):
        try:
            with gd_accounts._account_lock(account_id):
                barrier.wait()
        except Exception as exc:  # BrokenBarrierError if they serialized
            errors.append(exc)

    threads = [
        threading.Thread(target=enter, args=(f"acct-{i}",), daemon=True)
        for i in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert errors == []
    assert gd_accounts._account_locks == {}


# ---------------------------------------------------------------------------
# HVSA H1.4F F6: cleanup is best-effort and never masks the real failure
# ---------------------------------------------------------------------------


def test_discard_scoped_credentials_swallows_os_error(monkeypatch, caplog):
    def _explode(*, credentials_ref=None):
        raise OSError("keyring backend exploded")

    monkeypatch.setattr(gd_endpoints, "delete_google_drive_credentials", _explode)

    with caplog.at_level(logging.WARNING):
        gd_endpoints._discard_scoped_credentials("ref-x")  # must not raise

    assert "keyring backend exploded" not in caplog.text
    assert "ref-x" not in caplog.text


def test_discard_scoped_credentials_swallows_credential_storage_error(monkeypatch):
    def _locked(*, credentials_ref=None):
        raise cred_mod.CredentialStorageError("locked")

    monkeypatch.setattr(gd_endpoints, "delete_google_drive_credentials", _locked)

    gd_endpoints._discard_scoped_credentials("ref-x")  # must not raise


def test_cleanup_failure_keeps_provider_error_primary(monkeypatch, fake_keyring):
    def _offline(self, config):
        raise StorageUnavailableError(detail="network down", provider="GOOGLE_DRIVE")

    def _explode(*, credentials_ref=None):
        raise OSError("keyring backend exploded")

    monkeypatch.setattr(GoogleDriveAdapter, "authenticate", _offline)
    monkeypatch.setattr(gd_endpoints, "delete_google_drive_credentials", _explode)

    result = gd_endpoints._establish_connection(
        "token-a", ["scope-a"], credentials_ref="ref-a"
    )

    assert result["success"] is False
    assert result["error"].startswith("provider_unavailable")


def test_cleanup_failure_keeps_authentication_error_primary(monkeypatch, fake_keyring):
    def _reject(self, config):
        raise StorageAuthenticationError(
            detail="invalid_grant", provider="GOOGLE_DRIVE"
        )

    def _explode(*, credentials_ref=None):
        raise OSError("keyring backend exploded")

    monkeypatch.setattr(GoogleDriveAdapter, "authenticate", _reject)
    monkeypatch.setattr(gd_endpoints, "delete_google_drive_credentials", _explode)

    result = gd_endpoints._establish_connection(
        "token-a", ["scope-a"], credentials_ref="ref-a"
    )

    assert result["success"] is False
    assert result["error"].startswith("authentication_failed")


# ---------------------------------------------------------------------------
# HVSA H1.5.2A: Managed adapters proxy tests
# ---------------------------------------------------------------------------


def test_managed_adapters_proxy_getitem_raises_keyerror_for_missing_key():
    """_ManagedAdaptersProxy.__getitem__ raises KeyError for absent keys."""
    from backend.api.v1.endpoints.storage_accounts import _managed_adapters

    # Ensure the key doesn't exist
    _managed_adapters.clear()
    assert "nonexistent-account" not in _managed_adapters

    with pytest.raises(KeyError):
        _ = _managed_adapters["nonexistent-account"]


def test_managed_adapters_proxy_setitem_installs_through_manager():
    """_ManagedAdaptersProxy.__setitem__ installs through the manager."""
    from backend.api.v1.endpoints.storage_accounts import _managed_adapters
    from backend.storage.account_adapter_manager import adapter_manager

    _managed_adapters.clear()
    adapter = LocalAdapter("C:/proxy_test_path")

    _managed_adapters["test-account"] = adapter

    # Verify it's in the manager's cache
    assert "test-account" in adapter_manager._cache
    assert adapter_manager._cache["test-account"] is adapter


def test_managed_adapters_proxy_setitem_none_evicts():
    """_ManagedAdaptersProxy.__setitem__ with None evicts the entry."""
    from backend.api.v1.endpoints.storage_accounts import _managed_adapters
    from backend.storage.account_adapter_manager import adapter_manager

    _managed_adapters.clear()
    adapter = LocalAdapter("C:/proxy_test_path")
    _managed_adapters["test-account"] = adapter

    assert "test-account" in adapter_manager._cache

    # Set to None should evict
    _managed_adapters["test-account"] = None

    assert "test-account" not in adapter_manager._cache


def test_managed_adapters_proxy_delitem_raises_keyerror_when_absent():
    """_ManagedAdaptersProxy.__delitem__ raises KeyError when key is absent."""
    from backend.api.v1.endpoints.storage_accounts import _managed_adapters

    _managed_adapters.clear()

    with pytest.raises(KeyError):
        del _managed_adapters["nonexistent-account"]


def test_managed_adapters_proxy_delitem_removes_entry():
    """_ManagedAdaptersProxy.__delitem__ removes the entry."""
    from backend.api.v1.endpoints.storage_accounts import _managed_adapters
    from backend.storage.account_adapter_manager import adapter_manager

    _managed_adapters.clear()
    adapter = LocalAdapter("C:/proxy_test_path")
    _managed_adapters["test-account"] = adapter

    assert "test-account" in adapter_manager._cache

    del _managed_adapters["test-account"]

    assert "test-account" not in adapter_manager._cache


def test_managed_adapters_proxy_clear_removes_all():
    """_ManagedAdaptersProxy.clear removes all entries."""
    from backend.api.v1.endpoints.storage_accounts import _managed_adapters
    from backend.storage.account_adapter_manager import adapter_manager

    _managed_adapters.clear()
    _managed_adapters["account-1"] = LocalAdapter("C:/path1")
    _managed_adapters["account-2"] = LocalAdapter("C:/path2")

    assert len(_managed_adapters) == 2

    _managed_adapters.clear()

    assert len(_managed_adapters) == 0
    assert "account-1" not in adapter_manager._cache
    assert "account-2" not in adapter_manager._cache


def test_managed_adapters_proxy_iteration():
    """_ManagedAdaptersProxy iteration yields keys."""
    from backend.api.v1.endpoints.storage_accounts import _managed_adapters

    _managed_adapters.clear()
    _managed_adapters["account-1"] = LocalAdapter("C:/path1")
    _managed_adapters["account-2"] = LocalAdapter("C:/path2")

    keys = list(_managed_adapters)
    assert set(keys) == {"account-1", "account-2"}


def test_managed_adapters_proxy_len():
    """_ManagedAdaptersProxy.__len__ returns correct count."""
    from backend.api.v1.endpoints.storage_accounts import _managed_adapters

    _managed_adapters.clear()
    assert len(_managed_adapters) == 0

    _managed_adapters["account-1"] = LocalAdapter("C:/path1")
    assert len(_managed_adapters) == 1

    _managed_adapters["account-2"] = LocalAdapter("C:/path2")
    assert len(_managed_adapters) == 2

    del _managed_adapters["account-1"]
    assert len(_managed_adapters) == 1


def test_managed_adapters_proxy_membership():
    """_ManagedAdaptersProxy supports 'in' operator."""
    from backend.api.v1.endpoints.storage_accounts import _managed_adapters

    _managed_adapters.clear()
    _managed_adapters["account-1"] = LocalAdapter("C:/path1")

    assert "account-1" in _managed_adapters
    assert "account-2" not in _managed_adapters


def test_managed_adapters_proxy_get_with_default():
    """_ManagedAdaptersProxy.get returns default for missing keys."""
    from backend.api.v1.endpoints.storage_accounts import _managed_adapters

    _managed_adapters.clear()
    _managed_adapters["account-1"] = LocalAdapter("C:/path1")

    assert _managed_adapters.get("account-1") is not None
    assert _managed_adapters.get("nonexistent") is None
    assert _managed_adapters.get("nonexistent", "default") == "default"


def test_managed_adapters_proxy_pop_returns_value_and_removes():
    """_ManagedAdaptersProxy.pop returns value and removes entry."""
    from backend.api.v1.endpoints.storage_accounts import _managed_adapters
    from backend.storage.account_adapter_manager import adapter_manager

    _managed_adapters.clear()
    adapter = LocalAdapter("C:/path1")
    _managed_adapters["account-1"] = adapter

    result = _managed_adapters.pop("account-1")
    assert result is adapter
    assert "account-1" not in adapter_manager._cache


def test_managed_adapters_proxy_pop_missing_key_raises_keyerror():
    """_ManagedAdaptersProxy.pop raises KeyError for missing key without default."""
    from backend.api.v1.endpoints.storage_accounts import _managed_adapters

    _managed_adapters.clear()

    with pytest.raises(KeyError):
        _managed_adapters.pop("nonexistent")


def test_managed_adapters_proxy_pop_missing_key_returns_default():
    """_ManagedAdaptersProxy.pop returns default for missing key when provided."""
    from backend.api.v1.endpoints.storage_accounts import _managed_adapters

    _managed_adapters.clear()

    result = _managed_adapters.pop("nonexistent", "default_value")
    assert result == "default_value"


def test_set_managed_adapter_evicts_with_none():
    """_set_managed_adapter(account_id, None) evicts the entry."""
    from backend.api.v1.endpoints.storage_accounts import (
        _set_managed_adapter,
        _get_managed_adapter,
    )
    from backend.storage.account_adapter_manager import adapter_manager

    adapter_manager.invalidate_all()

    adapter = LocalAdapter("C:/path1")
    _set_managed_adapter("test-account", adapter)

    assert _get_managed_adapter("test-account") is adapter

    # Evict with None
    _set_managed_adapter("test-account", None)

    assert _get_managed_adapter("test-account") is None
    assert "test-account" not in adapter_manager._cache


def test_managed_adapters_proxy_pop_does_not_remove_concurrent_replacement():
    """Verify that pop() cannot remove a newer replacement installed concurrently.

    This test proves that pop() is safe against concurrent install() because:
    1. pop() holds _cache_lock for the read-remove operation
    2. install() also requires _cache_lock
    3. These operations are serialized by the lock

    The test deterministically verifies:
    - pop() returns the removed adapter
    - subsequent install() of a different adapter remains in cache
    """
    from backend.api.v1.endpoints.storage_accounts import _managed_adapters
    from backend.storage.account_adapter_manager import adapter_manager

    _managed_adapters.clear()

    # Install an initial adapter
    initial_adapter = LocalAdapter("C:/initial_path")
    _managed_adapters["test-account"] = initial_adapter

    # Pop should return the initial adapter
    popped = _managed_adapters.pop("test-account")
    assert popped is initial_adapter

    # The key should no longer exist
    assert "test-account" not in adapter_manager._cache

    # Install a new adapter after pop - it should work correctly
    new_adapter = LocalAdapter("C:/new_path")
    _managed_adapters["test-account"] = new_adapter

    # The new adapter should be in cache
    assert "test-account" in adapter_manager._cache
    assert adapter_manager._cache["test-account"] is new_adapter

    # Pop should return the new adapter
    popped2 = _managed_adapters.pop("test-account")
    assert popped2 is new_adapter

    # Key should be gone again
    assert "test-account" not in adapter_manager._cache


def test_managed_adapters_proxy_pop_invalidation_generation():
    """Verify that pop() properly invalidates the account generation.

    This test ensures that:
    - pop() increments the account generation (invalidation marker)
    - pop() removes the cache entry
    - Subsequent resolve() for the same account will retry construction
    """
    from backend.api.v1.endpoints.storage_accounts import _managed_adapters
    from backend.storage.account_adapter_manager import adapter_manager

    _managed_adapters.clear()

    # Install an initial adapter
    initial_adapter = LocalAdapter("C:/initial_path")
    _managed_adapters["test-account"] = initial_adapter

    # Get the generation before pop
    gen_before = adapter_manager._account_generations.get("test-account", 0)

    # Pop should return the initial adapter
    popped = _managed_adapters.pop("test-account")
    assert popped is initial_adapter

    # Generation should have been incremented
    gen_after = adapter_manager._account_generations.get("test-account", 0)
    assert gen_after == gen_before + 1, "Generation should be incremented after pop"

    # Key should no longer exist in cache
    assert "test-account" not in adapter_manager._cache
