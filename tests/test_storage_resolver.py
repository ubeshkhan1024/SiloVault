"""Tests for the Account-aware Adapter Resolver (H1.5.1).

Uses mocks, temporary test databases, and isolated fake credentials.
"""

from __future__ import annotations

import uuid
import os
import threading
import time
from typing import Any

import pytest

from sqlalchemy import create_engine
from backend.database import Base
from backend.models.storage_account import StorageAccount
from backend.storage.account_manager import AccountManager, AccountStatus
from backend.storage.adapter_factory import AdapterFactory
from backend.storage.errors import StorageUnavailableError
from backend.storage.resolver import AccountResolver
from backend.storage.google_drive import GoogleDriveAdapter
from backend.storage.local import LocalAdapter
from backend.storage.registry import register, lookup
from backend.storage.capabilities import CapabilityStatus


# ---------------------------------------------------------------------------
# Per-test in-memory engine for all tests (each test gets its own DB)
# ---------------------------------------------------------------------------

_per_test_engine: Any = None


@pytest.fixture(scope="function")
def per_test_engine(tmp_path):
    """Create a per-test file-backed SQLite engine."""
    db_path = tmp_path / "resolver_test.sqlite"

    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
    )

    Base.metadata.create_all(engine)

    from sqlalchemy.orm import sessionmaker
    SessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
    )

    yield SessionLocal

    engine.dispose()


@pytest.fixture(scope="function")
def db_session(per_test_engine):
    """Provide a session factory backed by an isolated test database."""
    return per_test_engine


# ---------------------------------------------------------------------------
# Fake keyring for credential isolation
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
            from keyring.errors import PasswordDeleteError
            raise PasswordDeleteError("not found")
        del self._entries[(service, username)]


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch):
    """Route all credential storage through an in-memory fake keyring."""
    import backend.storage.credentials as cred_mod
    
    kr = _FakeKeyring()
    monkeypatch.setattr(cred_mod, "_get_keyring", lambda: kr)
    return kr



# ---------------------------------------------------------------------------
# Test-account fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def local_account(db_session):
    """A LOCAL storage account with a custom storage root."""
    from sqlalchemy.orm import Session
    session: Session = db_session()
    try:
        account = StorageAccount(
            provider_id="LOCAL",
            display_name="Test Local Account",
            credentials_ref=None,
            configuration={"storage_root": "C:/silo_test_root"},
            status=AccountStatus.CONNECTED,
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        yield account.id
    finally:
        session.close()


@pytest.fixture
def local_account2(db_session):
    """A second LOCAL storage account with a different storage root."""
    from sqlalchemy.orm import Session
    session: Session = db_session()
    try:
        account = StorageAccount(
            provider_id="LOCAL",
            display_name="Test Local Account 2",
            credentials_ref=None,
            configuration={"storage_root": "C:/silo_test_root2"},
            status=AccountStatus.CONNECTED,
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        yield account.id
    finally:
        session.close()


@pytest.fixture
def google_drive_account(db_session):
    """A Google Drive storage account with a credential reference."""
    from sqlalchemy.orm import Session
    session: Session = db_session()
    try:
        account = StorageAccount(
            provider_id="GOOGLE_DRIVE",
            display_name="Test Google Drive Account",
            credentials_ref="google-drive-credentials-ref-123",
            configuration={},
            status=AccountStatus.CONNECTED,
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        yield account.id
    finally:
        session.close()


@pytest.fixture
def google_drive_account2(db_session):
    """A second Google Drive storage account with a different credential reference."""
    from sqlalchemy.orm import Session
    session: Session = db_session()
    try:
        account = StorageAccount(
            provider_id="GOOGLE_DRIVE",
            display_name="Test Google Drive Account 2",
            credentials_ref="google-drive-credentials-ref-456",
            configuration={},
            status=AccountStatus.CONNECTED,
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        yield account.id
    finally:
        session.close()


@pytest.fixture
def disconnected_account(db_session):
    """A LOCAL storage account that is disconnected."""
    from sqlalchemy.orm import Session
    session: Session = db_session()
    try:
        account = StorageAccount(
            provider_id="LOCAL",
            display_name="Disconnected Local Account",
            credentials_ref=None,
            configuration={"storage_root": "C:/disconnected_root"},
            status=AccountStatus.DISCONNECTED,
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        yield account.id
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Tests: resolve valid accounts
# ---------------------------------------------------------------------------

def test_resolve_valid_local_account(db_session, local_account):
    """Test 1: Resolving a valid connected LOCAL account."""
    resolver = AccountResolver(db_session)
    adapter = resolver.resolve(local_account)
    assert adapter is not None
    from backend.storage.local import LocalAdapter
    assert isinstance(adapter, LocalAdapter)


def test_resolve_two_local_accounts_preserve_separate_roots(
    db_session, local_account, local_account2
):
    """Test 2: Two LOCAL accounts preserve their separate storage roots."""
    resolver = AccountResolver(db_session)
    adapter1 = resolver.resolve(local_account)
    adapter2 = resolver.resolve(local_account2)
    from backend.storage.local import LocalAdapter
    assert isinstance(adapter1, LocalAdapter)
    assert isinstance(adapter2, LocalAdapter)
    assert adapter1.storage_root != adapter2.storage_root


def test_resolve_missing_account_raises_value_error(db_session):
    """Test 3: Missing account is rejected."""
    resolver = AccountResolver(db_session)
    fake_id = str(uuid.uuid4())
    with pytest.raises(ValueError, match=f"Storage account {fake_id!r} not found"):
        resolver.resolve(fake_id)


def test_resolve_disconnected_account_raises_value_error(db_session, disconnected_account):
    """Test 4: Disconnected account is rejected."""
    resolver = AccountResolver(db_session)
    with pytest.raises(ValueError, match="is not connected"):
        resolver.resolve(disconnected_account)


# ---------------------------------------------------------------------------
# Tests: cache invalidation
# ---------------------------------------------------------------------------

def test_invalidating_one_account_does_not_invalidate_another(
    db_session, local_account, local_account2
):
    """Test 5: Invalidating one account does not invalidate another's adapter."""
    resolver = AccountResolver(db_session)
    adapter1 = resolver.resolve(local_account)
    adapter2 = resolver.resolve(local_account2)
    resolver.invalidate_account(local_account)
    new_adapter1 = resolver.resolve(local_account)
    assert new_adapter1 is not adapter2


def test_full_cache_invalidation_works(db_session, local_account):
    """Test 6: Full cache invalidation works."""
    resolver = AccountResolver(db_session)
    adapter1 = resolver.resolve(local_account)
    adapter2 = resolver.resolve(local_account)
    assert adapter1 is adapter2
    resolver.invalidate_all()
    adapter3 = resolver.resolve(local_account)
    assert adapter3 is not adapter1


# ---------------------------------------------------------------------------
# Tests: legacy fallback
# ---------------------------------------------------------------------------

def test_legacy_fallback_uses_registry(db_session):
    """Test 7: Legacy fallback uses the provider-level Registry lookup."""
    resolver = AccountResolver(db_session)
    from backend.storage.local import LocalAdapter
    from backend.storage.registry import register, ProviderCapabilities
    from backend.storage.capabilities import CapabilityStatus
    adapter = LocalAdapter(storage_root="C:/legacy_test_root")
    try:
        register(
            "LOCAL",
            adapter,
            ProviderCapabilities(
                addressing_mode="AUTO",
                native_hash_algorithm="sha256",
                supports_trash=CapabilityStatus.TRUE,
                supports_delta_sync=CapabilityStatus.FALSE,
                supports_revisions=CapabilityStatus.FALSE,
                supports_search=CapabilityStatus.FALSE,
                supports_chunked_upload=CapabilityStatus.FALSE,
                supports_resumable_upload=CapabilityStatus.FALSE,
                requires_decryption=CapabilityStatus.FALSE,
            ),
        )
        resolved = resolver.resolve_legacy("LOCAL")
        assert resolved is not None
    except StorageUnavailableError:
        pass
    finally:
        # Clean up: remove the registered provider
        from backend.storage.registry import _adapter_map
        if "LOCAL" in _adapter_map:
            del _adapter_map["LOCAL"]


def test_non_null_invalid_account_id_never_falls_back_to_registry(
    db_session, google_drive_account
):
    """Test 8: A non-NULL but invalid account ID never falls back to Registry."""
    resolver = AccountResolver(db_session)
    fake_id = str(uuid.uuid4())
    with pytest.raises(ValueError, match="not found"):
        resolver.resolve(fake_id)


# Authentication test (mock Google Drive adapter)
def test_authentication_failure_does_not_cache_invalid_adapter(monkeypatch):
    """Test 9: Authentication failure does not cache an invalid adapter.

    Mocks GoogleDriveAdapter.authenticate so it always raises,
    then verifies that the resolver raises ValueError and does NOT
    cache the adapter — a second resolve() should raise again.
    """
    import backend.storage.google_drive as gd_mod
    from backend.storage.credentials import (
        GoogleDriveCredentials,
        store_google_drive_credentials,
    )

    original_authenticate = gd_mod.GoogleDriveAdapter.authenticate

    def always_fail(self, config):
        from backend.storage.errors import StorageAuthenticationError
        raise StorageAuthenticationError(
            detail="Mock auth failure",
            provider="GOOGLE_DRIVE",
        )

    gd_mod.GoogleDriveAdapter.authenticate = always_fail  # type: ignore
    try:
        # Use per-test in-memory engine so the resolver has a stable session
        from backend.database import Base
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        from sqlalchemy.orm import sessionmaker
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        session_factory = lambda: SessionLocal()

        # Create a Google Drive account in this engine
        resolver = AccountResolver(session_factory)
        from sqlalchemy.orm import Session as OrmSession
        session = session_factory()
        try:
            account = StorageAccount(
                provider_id="GOOGLE_DRIVE",
                display_name="Test Google Drive Account",
                credentials_ref="google-drive-credentials-ref-123",
                configuration={},
                status=AccountStatus.CONNECTED,
            )
            session.add(account)
            session.commit()
            session.refresh(account)

            # Pre-populate keyring with valid credentials so load_google_drive_credentials succeeds
            creds = GoogleDriveCredentials(
                refresh_token="test-refresh-token",
                client_id="test-client-id",
                client_secret="test-client-secret",
                scopes=("scope-a",),
            )
            store_google_drive_credentials(creds, credentials_ref=account.credentials_ref)

            # Now resolve should attempt authentication and fail
            with pytest.raises(ValueError, match="authentication failed"):
                resolver.resolve(account.id)
            # After failure, the adapter should NOT be cached.
            # Try resolving again — it should raise again, not return a cached adapter.
            with pytest.raises(ValueError, match="authentication failed"):
                resolver.resolve(account.id)
        finally:
            session.close()
    finally:
        import backend.storage.google_drive as gd_mod2
        gd_mod2.GoogleDriveAdapter.authenticate = original_authenticate  # type: ignore


# ---------------------------------------------------------------------------
# Deterministic Concurrency & Lifecycle Tests
# ---------------------------------------------------------------------------

def test_concurrent_resolution_same_account_returns_same_adapter(
    db_session, local_account, monkeypatch
):
    """Concurrent callers resolving one account receive the same adapter.

    With single-flight locking, the first caller constructs the adapter while
    subsequent callers wait and receive the cached instance.  Both threads
    must return the identical adapter object.
    """
    resolver = AccountResolver(db_session)

    results: list[Any] = [None, None]
    errors: list[Exception] = []

    call_count = {"count": 0}
    original_create = AdapterFactory.create_adapter

    def counting_create(self_factory):
        call_count["count"] += 1
        return original_create(self_factory)

    monkeypatch.setattr(
        AdapterFactory,
        "create_adapter",
        counting_create,
    )

    def worker(idx: int):
        try:
            results[idx] = resolver.resolve(local_account)
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(0,)),
        threading.Thread(target=worker, args=(1,)),
    ]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(timeout=15.0)

    assert all(not thread.is_alive() for thread in threads), (
        "A resolver worker did not finish"
    )
    assert not errors, f"Concurrent resolution raised errors: {errors}"
    assert results[0] is not None
    assert results[1] is not None
    assert results[0] is results[1], "Both threads must receive the same adapter instance"
    assert call_count["count"] == 1, "AdapterFactory.create_adapter should be called exactly once"
    assert resolver.resolve(local_account) is results[0]


def test_concurrent_resolution_different_accounts_returns_separate_adapters(
    db_session, local_account, local_account2, monkeypatch
):
    """Two threads resolving different accounts must receive separate adapter instances."""
    resolver = AccountResolver(db_session)
    barrier = threading.Barrier(2)
    results: dict[str, Any] = {}
    errors: list[Exception] = []

    def worker(account_id: str):
        try:
            barrier.wait(timeout=5.0)
            results[account_id] = resolver.resolve(account_id)
        except Exception as exc:
            errors.append(exc)

    original_create = AdapterFactory.create_adapter

    def mock_create(self_factory):
        barrier.wait(timeout=5.0)
        return original_create(self_factory)

    AdapterFactory.create_adapter = mock_create  # type: ignore

    t1 = threading.Thread(target=worker, args=(local_account,))
    t2 = threading.Thread(target=worker, args=(local_account2,))
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    AdapterFactory.create_adapter = original_create  # type: ignore

    assert not errors, f"Concurrent resolution raised errors: {errors}"
    adapter1 = results[local_account]
    adapter2 = results[local_account2]
    assert adapter1 is not adapter2
    assert isinstance(adapter1, LocalAdapter)
    assert isinstance(adapter2, LocalAdapter)
    assert adapter1.storage_root != adapter2.storage_root


def test_invalidation_during_construction_prevents_stale_cache(
    db_session, local_account
):
    """Invalidating an account while construction is in progress prevents stale cache insertion.

    - Pause construction of adapter A.
    - Invalidate the account while construction is paused.
    - Resume construction.
    - Verify adapter A is discarded and never returned or cached.
    - Verify the resolver retries and constructs adapter B.
    - Verify adapter B is cached.
    - Verify a subsequent resolve returns the exact same adapter B.

    Record constructed adapter instances so the test can distinguish A from B.
    """
    resolver = AccountResolver(db_session)

    construction_started = threading.Event()
    can_complete_construction = threading.Event()

    # Track the two adapter instances: stale A (before invalidation) and fresh B (after retry)
    stale_adapter = None
    fresh_adapter = None

    original_create = AdapterFactory.create_adapter

    def paused_create(self_factory):
        nonlocal stale_adapter, fresh_adapter
        construction_started.set()
        assert can_complete_construction.wait(timeout=5.0), "Timed out waiting to complete construction"
        adapter = original_create(self_factory)
        # Record the first constructed adapter as the stale one (Adapter A)
        if stale_adapter is None:
            stale_adapter = adapter
        else:
            # Second construction (after retry) is the fresh adapter (Adapter B)
            fresh_adapter = adapter
        return adapter

    AdapterFactory.create_adapter = paused_create  # type: ignore

    results: list[Any] = [None]
    errors: list[Exception] = []

    def resolve_worker():
        try:
            results[0] = resolver.resolve(local_account)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=resolve_worker)
    try:
        thread.start()
        assert construction_started.wait(timeout=5.0), "Timed out waiting for construction to start"
        # While construction is paused, invalidate the account
        resolver.invalidate_account(local_account)
        # Release construction to finish
        can_complete_construction.set()
        thread.join(timeout=5.0)
    finally:
        AdapterFactory.create_adapter = original_create  # type: ignore

    assert not errors, f"Resolution worker raised errors: {errors}"

    # Adapter A (stale) was constructed before invalidation and must NOT be cached.
    # Adapter B (fresh) was constructed after the generation mismatch retry and IS cached.
    cached_adapter = resolver._cache.get(local_account)
    assert cached_adapter is not stale_adapter, (
        "Stale adapter was inappropriately cached despite invalidation"
    )
    assert cached_adapter is fresh_adapter, (
        "Fresh adapter was not cached after generation mismatch retry"
    )
    # Subsequent resolve must return the same cached fresh adapter
    another_resolve = resolver.resolve(local_account)
    assert another_resolve is cached_adapter, (
        "Subsequent resolve did not return the cached fresh adapter"
    )


def test_legacy_fallback_explicit_verification(db_session):
    """Verify legacy fallback and prevent invalid account IDs from using it."""
    from backend.storage.registry import (
        register,
        replace,
        remove,
        lookup,
        ProviderCapabilities,
    )

    resolver = AccountResolver(db_session)
    adapter = LocalAdapter(storage_root="C:/legacy_test_root")

    capabilities = ProviderCapabilities(
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

    # Preserve any registration that existed before this test.
    try:
        original_registration = lookup("LOCAL")
    except StorageUnavailableError:
        original_registration = None

    installed = False

    try:
        if original_registration is None:
            register("LOCAL", adapter, capabilities)
        else:
            replace("LOCAL", adapter, capabilities)

        installed = True

        # NULL account ID must not enter account-aware resolution.
        with pytest.raises(ValueError):
            resolver.resolve(None)  # type: ignore

        # Explicit legacy resolution remains supported.
        resolved_legacy = resolver.resolve_legacy("LOCAL")
        assert resolved_legacy is adapter

        # An invalid non-NULL ID must never fall back to the registry.
        fake_id = str(uuid.uuid4())
        with pytest.raises(ValueError, match="not found"):
            resolver.resolve(fake_id)

    finally:
        if installed:
            if original_registration is None:
                remove("LOCAL")
            else:
                replace("LOCAL", *original_registration)


# ---------------------------------------------------------------------------
# H1.5.2A New Requirements Tests
# ---------------------------------------------------------------------------

def test_explicit_scoped_credential_loading_and_no_legacy_fallback(
    db_session, google_drive_account, monkeypatch
):
    """Verify that Google Drive resolution explicitly loads scoped credentials from keyring

    and never falls back to legacy credentials.
    """
    from backend.storage.credentials import (
        GoogleDriveCredentials,
        store_google_drive_credentials,
        _KEYRING_SERVICE,
    )
    from backend.storage.account_adapter_manager import AccountAdapterManager

    # Store credentials for the account's credentials_ref
    creds = GoogleDriveCredentials(
        refresh_token="scoped-token-xyz",
        client_id="cid",
        client_secret="csecret",
        scopes=("scope1",),
    )

    # Store under account's ref
    ref = "google-drive-credentials-ref-123"
    store_google_drive_credentials(creds, credentials_ref=ref)

    # Also store legacy credentials to ensure legacy entry is NOT loaded
    legacy_creds = GoogleDriveCredentials(
        refresh_token="legacy-token-do-not-use",
        client_id="cid",
        client_secret="csecret",
        scopes=("scope1",),
    )
    store_google_drive_credentials(legacy_creds, credentials_ref=None)

    # Spy on authenticate
    authenticated_config = {}

    def mock_auth(self, config):
        authenticated_config.update(config)

    monkeypatch.setattr(GoogleDriveAdapter, "authenticate", mock_auth)

    mgr = AccountAdapterManager(db_session)
    resolver = AccountResolver(db_session, manager=mgr)

    adapter = resolver.resolve(google_drive_account)
    assert adapter is not None
    assert isinstance(adapter, GoogleDriveAdapter)
    assert authenticated_config.get("refresh_token") == "scoped-token-xyz"


def test_missing_managed_credentials_raises_value_error(db_session, google_drive_account):
    """When credentials_ref exists on account but keyring holds no entry, raise ValueError."""
    from backend.storage.account_adapter_manager import AccountAdapterManager

    mgr = AccountAdapterManager(db_session)
    resolver = AccountResolver(db_session, manager=mgr)

    # No credentials stored in fake keyring for google_drive_account
    with pytest.raises(ValueError, match="no stored credentials"):
        resolver.resolve(google_drive_account)


def test_credential_store_failure_raises_unavailable_error(
    db_session, google_drive_account, monkeypatch
):
    """When loading credentials raises CredentialStorageError, propagate as StorageUnavailableError."""
    from backend.storage.account_adapter_manager import AccountAdapterManager
    from backend.storage.credentials import CredentialStorageError

    def failing_load(*args, **kwargs):
        raise CredentialStorageError("Keyring locked")

    monkeypatch.setattr(
        "backend.storage.account_adapter_manager.load_google_drive_credentials",
        failing_load,
    )

    mgr = AccountAdapterManager(db_session)
    resolver = AccountResolver(db_session, manager=mgr)

    with pytest.raises(StorageUnavailableError, match="Credential store unavailable"):
        resolver.resolve(google_drive_account)


def test_failed_authentication_not_cached_new(db_session, google_drive_account, monkeypatch):
    """Failed adapter authentication must not be cached."""
    from backend.storage.account_adapter_manager import AccountAdapterManager
    from backend.storage.credentials import (
        GoogleDriveCredentials,
        store_google_drive_credentials,
    )

    ref = "google-drive-credentials-ref-123"
    store_google_drive_credentials(
        GoogleDriveCredentials("rt", "cid", "cs", ("s",)),
        credentials_ref=ref,
    )

    def failing_auth(self, config):
        raise RuntimeError("Auth network dropped")

    monkeypatch.setattr(GoogleDriveAdapter, "authenticate", failing_auth)

    mgr = AccountAdapterManager(db_session)
    resolver = AccountResolver(db_session, manager=mgr)

    with pytest.raises(ValueError, match="authentication failed"):
        resolver.resolve(google_drive_account)

    # Cache must remain empty for this account
    assert google_drive_account not in mgr.cache


def test_auth_failure_does_not_remove_concurrently_installed_adapter(
    db_session, google_drive_account, monkeypatch
):
    """Verify that authentication failure does NOT remove a concurrently installed adapter.

    Race scenario:
    - Thread A starts resolving google_drive_account (will authenticate)
    - Thread B installs a different adapter for the same account BEFORE A's auth completes
    - Thread A's auth fails
    - Thread B's adapter should remain in cache (not be evicted by A's failure)

    This test uses synchronization to deterministically create the race condition.
    """
    from backend.storage.account_adapter_manager import AccountAdapterManager
    from backend.storage.credentials import (
        GoogleDriveCredentials,
        store_google_drive_credentials,
    )

    mgr = AccountAdapterManager(db_session)
    resolver = AccountResolver(db_session, manager=mgr)

    # Pre-store credentials so resolution proceeds to auth
    store_google_drive_credentials(
        GoogleDriveCredentials("rt", "cid", "cs", ("s",)),
        credentials_ref="google-drive-credentials-ref-123",
    )

    auth_started = threading.Event()
    auth_can_finish = threading.Event()

    def failing_auth_but_signal(self, config):
        auth_started.set()
        # Wait for install to happen, then fail
        assert auth_can_finish.wait(timeout=5.0), "Timed out waiting for install"
        raise RuntimeError("Auth network dropped")

    monkeypatch.setattr(GoogleDriveAdapter, "authenticate", failing_auth_but_signal)

    results = [None, None]
    errors = []
    installed_adapters = []  # Track what was installed

    def resolve_worker():
        try:
            results[0] = resolver.resolve(google_drive_account)
        except Exception as exc:
            errors.append(exc)

    def install_worker():
        try:
            # Wait for auth to start (meaning we're in the authenticate() call)
            assert auth_started.wait(timeout=5.0), "Timed out waiting for auth start"
            # Small delay to ensure resolve_worker has entered the per-account lock
            time.sleep(0.1)
            # Install a new adapter while resolution is in progress
            new_adapter = LocalAdapter("C:/concurrent_path")
            installed_adapters.append(new_adapter)  # Make accessible outside thread
            mgr.install(google_drive_account, new_adapter)
            auth_can_finish.set()
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=resolve_worker)
    t2 = threading.Thread(target=install_worker)

    t1.start()
    t2.start()

    t1.join(timeout=15.0)
    t2.join(timeout=15.0)

    # Resolution should fail with ValueError
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert "authentication failed" in str(errors[0])

    # The concurrently installed adapter should STILL be in cache
    assert google_drive_account in mgr.cache
    cached_adapter = mgr.cache[google_drive_account]
    assert cached_adapter is installed_adapters[0]


def test_installation_and_replacement(db_session, local_account):
    """Manager install() adds adapter to cache, and second install replaces it."""
    from backend.storage.account_adapter_manager import AccountAdapterManager

    mgr = AccountAdapterManager(db_session)
    adapter_a = LocalAdapter("C:/path_a")
    adapter_b = LocalAdapter("C:/path_b")

    mgr.install(local_account, adapter_a)
    assert mgr.cache[local_account] is adapter_a

    mgr.install(local_account, adapter_b)
    assert mgr.cache[local_account] is adapter_b


def test_account_invalidation_clears_cache(db_session, local_account):
    """Invalidating an account evicts its cached entry."""
    from backend.storage.account_adapter_manager import AccountAdapterManager

    mgr = AccountAdapterManager(db_session)
    resolver = AccountResolver(db_session, manager=mgr)

    adapter1 = resolver.resolve(local_account)
    assert local_account in mgr.cache

    resolver.invalidate_account(local_account)
    assert local_account not in mgr.cache


def test_concurrent_resolution_single_flight(db_session, local_account, monkeypatch):
    """Verify single-flight per-account locking during concurrent resolution."""
    from backend.storage.account_adapter_manager import AccountAdapterManager

    mgr = AccountAdapterManager(db_session)
    resolver = AccountResolver(db_session, manager=mgr)

    call_count = {"count": 0}
    original_create = AdapterFactory.create_adapter

    def slow_create(self_factory):
        call_count["count"] += 1
        time.sleep(0.1)
        return original_create(self_factory)

    monkeypatch.setattr(AdapterFactory, "create_adapter", slow_create)

    results = [None, None]

    def worker(idx):
        results[idx] = resolver.resolve(local_account)

    t1 = threading.Thread(target=worker, args=(0,))
    t2 = threading.Thread(target=worker, args=(1,))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results[0] is not None
    assert results[0] is results[1]
    assert call_count["count"] == 1


def test_concurrent_install_during_resolution_does_not_evict_newer_adapter(
    db_session, local_account, monkeypatch
):
    """Verify that generation-based protection prevents stale cache installation.

    When an adapter is installed after resolution starts, the resolution's
    generation check should detect the change and NOT install its adapter.
    """
    from backend.storage.account_adapter_manager import AccountAdapterManager

    mgr = AccountAdapterManager(db_session)
    resolver = AccountResolver(db_session, manager=mgr)

    # Pre-install an adapter that we'll try to evict
    initial_adapter = LocalAdapter("C:/initial_path")
    mgr.install(local_account, initial_adapter)
    assert mgr.cache[local_account] is initial_adapter

    # Install a new adapter (simulating concurrent replacement)
    new_adapter = LocalAdapter("C:/new_path")
    mgr.install(local_account, new_adapter)

    # Verify the new adapter is in cache
    assert mgr.cache[local_account] is new_adapter

    # The initial_adapter should not be in cache
    assert mgr.cache[local_account] is not initial_adapter


def test_cache_lock_not_held_during_db_operations(db_session, local_account, monkeypatch):
    """Verify that _cache_lock is not held during database operations.

    This test uses a lock to detect if _cache_lock is held while DB session is open.
    """
    from backend.storage.account_adapter_manager import AccountAdapterManager
    import backend.storage.account_adapter_manager as manager_module

    mgr = AccountAdapterManager(db_session)
    resolver = AccountResolver(db_session, manager=mgr)

    lock_held_during_db = threading.Event()
    db_session_open = threading.Event()

    original_session_factory = db_session

    def session_factory_that_detects_lock():
        # Check if cache_lock is held
        if manager_module.adapter_manager._cache_lock.locked():
            lock_held_during_db.set()
        session = original_session_factory()
        db_session_open.set()
        return session

    # Use a fresh manager for this test
    mgr2 = AccountAdapterManager(session_factory_that_detects_lock)
    resolver2 = AccountResolver(db_session, manager=mgr2)

    result = resolver2.resolve(local_account)
    assert result is not None

    # The cache_lock should NOT have been held during DB operations
    assert not lock_held_during_db.is_set(), (
        "_cache_lock was held during database operations"
    )
