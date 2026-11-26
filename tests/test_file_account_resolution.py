"""Tests for fail-closed file adapter resolution (HVSA H1.5.4 Step 1).

Covers the routing contract:

* ``provider_account_id is None`` -> legacy provider registry.
* ``provider_account_id`` set -> that exact managed account.
* Unknown / disconnected / mismatched accounts fail explicitly.
* Managed failures never consult the legacy registry.

All tests use an isolated SQLite database and fakes; no credentials, provider
calls, or real application database are involved.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.storage_account import StorageAccount
from backend.storage import file_account_resolution as resolution
from backend.storage.account_adapter_manager import AccountAdapterManager
from backend.storage.capabilities import CAPABILITY_MAP
from backend.storage.errors import StorageUnavailableError
from backend.storage.file_account_resolution import (
    StorageAccountNotFoundError,
    StorageAccountNotConnectedError,
    StorageAccountProviderMismatchError,
    resolve_file_adapter,
)
from backend.storage.local import LocalAdapter
from backend.storage.registry import _adapter_map, _capability_map, register


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory(tmp_path):
    """Isolated file-backed database shared by accounts and the helper."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'resolution.db').as_posix()}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield Session
    finally:
        engine.dispose()


@pytest.fixture
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


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


class _FakeManager:
    """Records resolve calls and returns/raises exactly what the test asks."""

    def __init__(self, *, adapters=None, error=None):
        self.adapters = adapters or {}
        self.error = error
        self.calls: list[str] = []

    def resolve(self, account_id: str):
        self.calls.append(account_id)
        if self.error is not None:
            raise self.error
        return self.adapters[account_id]


def make_account(
    db,
    *,
    provider_id: str = "LOCAL",
    display_name: str = "Account",
    credentials_ref: str | None = None,
    configuration: dict | None = None,
    status: str = "connected",
) -> StorageAccount:
    account = StorageAccount(
        provider_id=provider_id,
        display_name=display_name,
        credentials_ref=credentials_ref,
        configuration=configuration or {},
        status=status,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def record(provider: str, account_id: str | None):
    return SimpleNamespace(provider=provider, provider_account_id=account_id)


def forbid_registry(monkeypatch) -> list[str]:
    """Fail the test if the helper consults the legacy registry."""
    calls: list[str] = []

    def _fail(provider):
        calls.append(provider)
        raise AssertionError(
            "legacy provider registry must not be consulted for a managed record"
        )

    monkeypatch.setattr(resolution, "lookup", _fail)
    return calls


# ---------------------------------------------------------------------------
# Legacy (NULL account) routing
# ---------------------------------------------------------------------------


def test_null_account_resolves_through_legacy_registry(db):
    _adapter_map.pop("LOCAL", None)
    _capability_map.pop("LOCAL", None)
    legacy_adapter = LocalAdapter(storage_root="/legacy/root")
    register("LOCAL", legacy_adapter, CAPABILITY_MAP["LOCAL"])
    manager = _FakeManager(error=AssertionError("manager must not be used"))

    resolved = resolve_file_adapter(db, record("LOCAL", None), manager=manager)

    assert resolved is legacy_adapter
    assert manager.calls == []


def test_legacy_resolution_propagates_registry_unavailable(db, monkeypatch):
    _adapter_map.pop("LOCAL", None)
    _capability_map.pop("LOCAL", None)
    manager = _FakeManager(error=AssertionError("manager must not be used"))

    with pytest.raises(StorageUnavailableError) as excinfo:
        resolve_file_adapter(db, record("LOCAL", None), manager=manager)

    assert excinfo.value.provider == "LOCAL"
    assert manager.calls == []


# ---------------------------------------------------------------------------
# Managed-account routing
# ---------------------------------------------------------------------------


def test_valid_managed_account_resolves_through_manager(db):
    account = make_account(db, provider_id="LOCAL", configuration={"storage_root": "/data/a"})
    exact_adapter = object()
    manager = _FakeManager(adapters={account.id: exact_adapter})

    resolved = resolve_file_adapter(db, record("LOCAL", account.id), manager=manager)

    assert resolved is exact_adapter
    assert manager.calls == [account.id]


def test_account_a_never_returns_account_b_adapter(db):
    account_a = make_account(db, display_name="A", configuration={"storage_root": "/data/a"})
    account_b = make_account(db, display_name="B", configuration={"storage_root": "/data/b"})
    adapter_a = object()
    adapter_b = object()
    manager = _FakeManager(adapters={account_a.id: adapter_a, account_b.id: adapter_b})

    resolved_a = resolve_file_adapter(db, record("LOCAL", account_a.id), manager=manager)
    resolved_b = resolve_file_adapter(db, record("LOCAL", account_b.id), manager=manager)

    assert resolved_a is adapter_a
    assert resolved_b is adapter_b
    assert resolved_a is not resolved_b
    assert manager.calls == [account_a.id, account_b.id]


# ---------------------------------------------------------------------------
# Fail-closed behavior
# ---------------------------------------------------------------------------


def test_unknown_account_fails_without_touching_manager_or_registry(db, monkeypatch):
    registry_calls = forbid_registry(monkeypatch)
    manager = _FakeManager(error=AssertionError("manager must not be used"))
    missing_id = str(uuid.uuid4())

    with pytest.raises(StorageAccountNotFoundError) as excinfo:
        resolve_file_adapter(db, record("GOOGLE_DRIVE", missing_id), manager=manager)

    assert excinfo.value.account_id == missing_id
    assert excinfo.value.provider == "GOOGLE_DRIVE"
    assert manager.calls == []
    assert registry_calls == []


def test_disconnected_account_fails_without_registry_fallback(db, session_factory, monkeypatch):
    account = make_account(db, status="disconnected", configuration={"storage_root": "/data/a"})
    registry_calls = forbid_registry(monkeypatch)
    manager = AccountAdapterManager(session_factory)

    with pytest.raises(StorageAccountNotConnectedError) as excinfo:
        resolve_file_adapter(db, record("LOCAL", account.id), manager=manager)

    assert excinfo.value.account_id == account.id
    assert isinstance(excinfo.value.__cause__, ValueError)
    assert registry_calls == []


def test_provider_mismatch_fails_before_manager_resolution(db, monkeypatch):
    account = make_account(db, provider_id="LOCAL", configuration={"storage_root": "/data/a"})
    registry_calls = forbid_registry(monkeypatch)
    manager = _FakeManager(adapters={account.id: object()})

    with pytest.raises(StorageAccountProviderMismatchError) as excinfo:
        resolve_file_adapter(db, record("GOOGLE_DRIVE", account.id), manager=manager)

    assert excinfo.value.account_id == account.id
    assert excinfo.value.expected_provider == "GOOGLE_DRIVE"
    assert excinfo.value.actual_provider == "LOCAL"
    assert manager.calls == []
    assert registry_calls == []


def test_managed_value_error_never_falls_back_to_registry(db, monkeypatch):
    account = make_account(db, provider_id="GOOGLE_DRIVE", credentials_ref="ref-a")
    registry_calls = forbid_registry(monkeypatch)
    manager = _FakeManager(error=ValueError("account is not connected"))

    with pytest.raises(StorageAccountNotConnectedError) as excinfo:
        resolve_file_adapter(db, record("GOOGLE_DRIVE", account.id), manager=manager)

    assert isinstance(excinfo.value.__cause__, ValueError)
    assert manager.calls == [account.id]
    assert registry_calls == []


def test_managed_storage_unavailable_propagates_unchanged(db, monkeypatch):
    account = make_account(db, provider_id="GOOGLE_DRIVE", credentials_ref="ref-a")
    registry_calls = forbid_registry(monkeypatch)
    original = StorageUnavailableError(
        detail="credential store unavailable", provider="GOOGLE_DRIVE"
    )
    manager = _FakeManager(error=original)

    with pytest.raises(StorageUnavailableError) as excinfo:
        resolve_file_adapter(db, record("GOOGLE_DRIVE", account.id), manager=manager)

    assert excinfo.value is original
    assert manager.calls == [account.id]
    assert registry_calls == []


def test_missing_credentials_fail_closed(db, session_factory, monkeypatch):
    """A connected account without usable credentials is not connected."""
    account = make_account(
        db,
        provider_id="GOOGLE_DRIVE",
        credentials_ref="ref-a",
        status="connected",
    )
    registry_calls = forbid_registry(monkeypatch)
    monkeypatch.setattr(
        "backend.storage.account_adapter_manager.load_google_drive_credentials",
        lambda credentials_ref=None: None,
    )
    manager = AccountAdapterManager(session_factory)

    with pytest.raises(StorageAccountNotConnectedError) as excinfo:
        resolve_file_adapter(db, record("GOOGLE_DRIVE", account.id), manager=manager)

    assert "no stored credentials" in str(excinfo.value.__cause__)
    assert registry_calls == []
