"""Cross-account isolation tests for account-scoped adapter resolution.

Covers ``AccountAdapterManager`` (through the ``AccountResolver`` façade):

* invalidating one account never evicts another account's cached adapter,
* two Google Drive accounts authenticate with their own keyring entries,
* disconnecting one account leaves the other account's adapter cached,
* managed resolution never modifies or replaces the provider registry.

The keyring is replaced with an in-memory fake and Google Drive
authentication is mocked, so no OS credential store, provider API, OAuth
flow, or real application database is used.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.storage_account import StorageAccount
from backend.storage import credentials as cred_mod
from backend.storage.account_manager import AccountManager, AccountStatus
from backend.storage.credentials import (
    GoogleDriveCredentials,
    store_google_drive_credentials,
)
from backend.storage.google_drive import GoogleDriveAdapter
from backend.storage.local import LocalAdapter
from backend.storage.registry import (
    _adapter_map,
    _capability_map,
    ProviderCapabilities,
    lookup,
    register,
)
from backend.storage.capabilities import CapabilityStatus
from backend.storage.resolver import AccountResolver


# ---------------------------------------------------------------------------
# Fixtures
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
        from keyring.errors import PasswordDeleteError

        if (service, username) not in self._entries:
            raise PasswordDeleteError("not found")
        del self._entries[(service, username)]


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    kr = _FakeKeyring()
    monkeypatch.setattr(cred_mod, "_get_keyring", lambda: kr)
    return kr


@pytest.fixture
def session_factory(tmp_path):
    """Isolated file-backed database shared by the resolver and the test."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'isolation.db').as_posix()}")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield TestSession
    finally:
        engine.dispose()


@pytest.fixture
def resolver(session_factory):
    """A resolver with its own manager instance (no process-wide cache)."""
    return AccountResolver(session_factory)


@pytest.fixture(autouse=True)
def clean_registry():
    """Restore the process-wide provider registry after each test."""
    adapters = dict(_adapter_map)
    capabilities = dict(_capability_map)
    try:
        yield
    finally:
        _adapter_map.clear()
        _adapter_map.update(adapters)
        _capability_map.clear()
        _capability_map.update(capabilities)


def make_account(
    session_factory,
    *,
    provider_id: str = "LOCAL",
    display_name: str = "Account",
    credentials_ref: str | None = None,
    configuration: dict[str, Any] | None = None,
    status: str = AccountStatus.CONNECTED,
) -> str:
    session = session_factory()
    try:
        account = StorageAccount(
            provider_id=provider_id,
            display_name=display_name,
            credentials_ref=credentials_ref,
            configuration=configuration or {},
            status=status,
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        return account.id
    finally:
        session.close()


def drive_credentials(refresh_token: str) -> GoogleDriveCredentials:
    return GoogleDriveCredentials(
        refresh_token=refresh_token,
        client_id="client-id",
        client_secret="client-secret",
        scopes=("scope-a",),
    )


def record_authentication(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Replace provider authentication with a recorder keyed by token."""
    seen: dict[str, str] = {}

    def fake_authenticate(self, config):
        seen[self._credentials_ref] = config["refresh_token"]

    monkeypatch.setattr(GoogleDriveAdapter, "authenticate", fake_authenticate)
    return seen


def local_capabilities() -> ProviderCapabilities:
    return ProviderCapabilities(
        addressing_mode="AUTO",
        native_hash_algorithm="sha256",
        supports_trash=CapabilityStatus.TRUE,
        supports_delta_sync=CapabilityStatus.FALSE,
        supports_revisions=CapabilityStatus.FALSE,
        supports_search=CapabilityStatus.FALSE,
        supports_chunked_upload=CapabilityStatus.FALSE,
        supports_resumable_upload=CapabilityStatus.FALSE,
        requires_decryption=CapabilityStatus.FALSE,
    )


# ---------------------------------------------------------------------------
# Cache isolation
# ---------------------------------------------------------------------------


def test_invalidating_a_keeps_bs_cached_adapter_instance(resolver, session_factory):
    account_a = make_account(
        session_factory, display_name="A", configuration={"storage_root": "/data/a"}
    )
    account_b = make_account(
        session_factory, display_name="B", configuration={"storage_root": "/data/b"}
    )

    adapter_a = resolver.resolve(account_a)
    adapter_b = resolver.resolve(account_b)
    assert adapter_a is not adapter_b

    resolver.invalidate_account(account_a)

    assert resolver.resolve(account_a) is not adapter_a
    # B's exact cached instance survives A's invalidation.
    assert resolver.resolve(account_b) is adapter_b


def test_disconnecting_a_does_not_evict_bs_cached_adapter(resolver, session_factory):
    account_a = make_account(
        session_factory, display_name="A", configuration={"storage_root": "/data/a"}
    )
    account_b = make_account(
        session_factory, display_name="B", configuration={"storage_root": "/data/b"}
    )
    adapter_b = resolver.resolve(account_b)

    # What the disconnect endpoint does: mark the row disconnected and drop
    # only that account's cached adapter.
    session = session_factory()
    try:
        AccountManager(session).disconnect_account(account_a)
    finally:
        session.close()
    resolver.invalidate_account(account_a)

    assert resolver.cache.get(account_a) is None
    assert resolver.resolve(account_b) is adapter_b


# ---------------------------------------------------------------------------
# Per-account credential isolation
# ---------------------------------------------------------------------------


def test_two_google_drive_accounts_authenticate_with_their_own_tokens(
    resolver, session_factory, monkeypatch
):
    account_a = make_account(
        session_factory,
        provider_id="GOOGLE_DRIVE",
        display_name="A",
        credentials_ref="ref-a",
    )
    account_b = make_account(
        session_factory,
        provider_id="GOOGLE_DRIVE",
        display_name="B",
        credentials_ref="ref-b",
    )
    store_google_drive_credentials(drive_credentials("token-a"), credentials_ref="ref-a")
    store_google_drive_credentials(drive_credentials("token-b"), credentials_ref="ref-b")
    seen = record_authentication(monkeypatch)

    adapter_a = resolver.resolve(account_a)
    adapter_b = resolver.resolve(account_b)

    assert isinstance(adapter_a, GoogleDriveAdapter)
    assert isinstance(adapter_b, GoogleDriveAdapter)
    assert adapter_a is not adapter_b
    assert adapter_a._credentials_ref == "ref-a"
    assert adapter_b._credentials_ref == "ref-b"
    assert seen == {"ref-a": "token-a", "ref-b": "token-b"}


# ---------------------------------------------------------------------------
# Provider registry boundary
# ---------------------------------------------------------------------------


def test_managed_resolution_does_not_modify_the_provider_registry(
    resolver, session_factory
):
    _adapter_map.pop("LOCAL", None)
    _capability_map.pop("LOCAL", None)
    legacy_adapter = LocalAdapter(storage_root="/legacy/root")
    register("LOCAL", legacy_adapter, local_capabilities())
    before = dict(_adapter_map)

    account = make_account(
        session_factory, display_name="Managed", configuration={"storage_root": "/data/managed"}
    )
    managed_adapter = resolver.resolve(account)

    assert isinstance(managed_adapter, LocalAdapter)
    assert managed_adapter is not legacy_adapter
    # The registry still holds exactly the legacy registration, and lookup
    # returns the legacy adapter, never the managed one.
    assert dict(_adapter_map) == before
    assert lookup("LOCAL")[0] is legacy_adapter
    assert managed_adapter not in _adapter_map.values()
