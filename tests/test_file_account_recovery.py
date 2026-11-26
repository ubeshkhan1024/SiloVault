"""Account-aware recovery tests (HVSA H1.5.4 Step 3C).

Verifies that ``RecoveryService`` resolves CLEANUP and RESTORE adapters with
``resolve_file_adapter``:

* legacy NULL-account records keep using the provider registry,
* managed records use exactly the account named on ``PendingDeletion``,
* resolution failures (unknown, disconnected, mismatched, unavailable) never
  fall back to the registry, never finalize the row, and never turn into a
  "missing file" success,
* a genuinely absent resource in the *correct* account keeps the existing
  idempotent cleanup behavior,
* ownership, claim tokens, leases, generation fencing, and retry scheduling
  survive every transition.

Isolated file-backed SQLite databases and fake or temporary-directory
adapters only; no credentials, keyring, or provider APIs.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.pending_deletion import PendingDeletion
from backend.models.storage_account import StorageAccount
from backend.services.recovery import RecoveryService
from backend.storage.account_adapter_manager import adapter_manager
from backend.storage.capabilities import CAPABILITY_MAP
from backend.storage.errors import (
    StorageNotFoundError,
    StorageUnavailableError,
)
from backend.storage.interfaces import StorageOperations
from backend.storage.local import LocalAdapter
from backend.storage.registry import _adapter_map, _capability_map, register
from backend.storage.types import ProviderFileRecord


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRecoveryAdapter(StorageOperations):
    """Account- or provider-level fake recording recovery calls."""

    def __init__(self, tag: str):
        self.tag = tag
        self.cleaned_ids: list[str] = []
        self.restored_ids: list[str] = []
        self.download_calls: list[str] = []
        self.cleanup_exc: Exception | None = None
        self.restore_exc: Exception | None = None
        self.download_data: bytes | None = None

    def upload(self, file_data, name, logical_path):
        raise NotImplementedError

    def download(self, provider_file_id: str) -> bytes:
        self.download_calls.append(provider_file_id)
        if self.download_data is None:
            raise NotImplementedError
        return self.download_data

    def quarantine(self, provider_file_id: str) -> str:
        raise NotImplementedError

    def restore(self, quarantine_id: str) -> ProviderFileRecord:
        if self.restore_exc is not None:
            raise self.restore_exc
        self.restored_ids.append(quarantine_id)
        return ProviderFileRecord(
            provider_file_id="restored",
            name="restored",
            logical_path="/restored",
            mime_type="application/octet-stream",
            size_bytes=0,
        )

    def rename(self, provider_file_id: str, new_name: str) -> ProviderFileRecord:
        raise NotImplementedError

    def move(
        self, source_provider_file_id: str, target_logical_path: str
    ) -> ProviderFileRecord:
        raise NotImplementedError

    def cleanup(self, quarantine_id: str) -> None:
        if self.cleanup_exc is not None:
            raise self.cleanup_exc
        self.cleaned_ids.append(quarantine_id)


class _ExplodingRegistryAdapter(StorageOperations):
    """Legacy registry adapter that records and refuses any use."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def upload(self, file_data, name, logical_path):
        raise NotImplementedError

    def download(self, provider_file_id: str) -> bytes:
        raise NotImplementedError

    def quarantine(self, provider_file_id: str) -> str:
        self.calls.append(("quarantine", provider_file_id))
        raise AssertionError("managed record must not use the legacy registry")

    def restore(self, quarantine_id: str):
        self.calls.append(("restore", quarantine_id))
        raise AssertionError("managed record must not use the legacy registry")

    def rename(self, provider_file_id: str, new_name: str) -> ProviderFileRecord:
        raise NotImplementedError

    def move(
        self, source_provider_file_id: str, target_logical_path: str
    ) -> ProviderFileRecord:
        raise NotImplementedError

    def cleanup(self, quarantine_id: str) -> None:
        self.calls.append(("cleanup", quarantine_id))
        raise AssertionError("managed record must not use the legacy registry")


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


@pytest.fixture
def recovery_env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'recovery.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(adapter_manager, "_session_factory", Session)
    adapter_manager.invalidate_all()

    try:
        yield SimpleNamespace(
            engine=engine,
            Session=Session,
            service=RecoveryService(lambda: Session()),
        )
    finally:
        adapter_manager.invalidate_all()
        engine.dispose()


@pytest.fixture(autouse=True)
def clean_registry():
    adapters = dict(_adapter_map)
    capabilities = dict(_capability_map)
    try:
        yield
    finally:
        _adapter_map.clear()
        _adapter_map.update(adapters)
        _capability_map.clear()
        _capability_map.update(capabilities)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def set_registry_adapter(provider: str, adapter) -> None:
    _adapter_map[provider] = adapter
    _capability_map[provider] = CAPABILITY_MAP.get(provider, CAPABILITY_MAP["LOCAL"])


def seed_account(
    env,
    provider_id: str = "LOCAL",
    *,
    status: str = "connected",
    configuration: dict | None = None,
    account_id: str | None = None,
) -> str:
    session = env.Session()
    try:
        account = StorageAccount(
            id=account_id or str(uuid.uuid4()),
            provider_id=provider_id,
            display_name=f"{provider_id} account",
            configuration=configuration or {},
            status=status,
        )
        session.add(account)
        session.commit()
        return account.id
    finally:
        session.close()


def delete_account(env, account_id: str) -> None:
    session = env.Session()
    try:
        account = session.get(StorageAccount, account_id)
        if account is not None:
            session.delete(account)
            session.commit()
    finally:
        session.close()


def seed_pending(
    env,
    provider: str = "LOCAL",
    account_id: str | None = None,
    *,
    operation_type: str = "CLEANUP",
    provider_file_id: str = "file-1",
    quarantine_id: str = "q-1",
    file_name: str = "file.bin",
    status: str = "PENDING",
    retry_count: int = 0,
    claim_token: str | None = None,
    lease_expires_at: datetime | None = None,
    next_retry_at: datetime | None = None,
    original_sha256: str | None = None,
) -> str:
    session = env.Session()
    try:
        row = PendingDeletion(
            provider=provider,
            provider_account_id=account_id,
            provider_file_id=provider_file_id,
            quarantine_id=quarantine_id,
            file_name_snapshot=file_name,
            status=status,
            retry_count=retry_count,
            claim_token=claim_token,
            lease_expires_at=lease_expires_at,
            next_retry_at=next_retry_at,
            operation_type=operation_type,
            operation_generation=0,
            original_sha256=original_sha256,
        )
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


def fetch_pending(env, row_id: str) -> PendingDeletion | None:
    session = env.Session()
    try:
        return session.get(PendingDeletion, row_id)
    finally:
        session.close()


def all_pending(env) -> list[PendingDeletion]:
    session = env.Session()
    try:
        return list(session.execute(select(PendingDeletion)).scalars().all())
    finally:
        session.close()


def make_local_root(tmp_path, name: str, provider_file_id: str, data: bytes):
    root = tmp_path / name
    root.mkdir()
    (root / provider_file_id).write_bytes(data)
    return root


# ---------------------------------------------------------------------------
# A. Legacy compatibility
# ---------------------------------------------------------------------------


def test_legacy_local_cleanup_uses_registry(recovery_env):
    adapter = _FakeRecoveryAdapter("legacy-local")
    set_registry_adapter("LOCAL", adapter)
    row_id = seed_pending(recovery_env, "LOCAL", None, quarantine_id="q-legacy-local")

    assert recovery_env.service.process_pending_deletions() == 1

    assert adapter.cleaned_ids == ["q-legacy-local"]
    assert fetch_pending(recovery_env, row_id) is None


def test_legacy_drive_cleanup_uses_registry(recovery_env):
    adapter = _FakeRecoveryAdapter("legacy-drive")
    set_registry_adapter("GOOGLE_DRIVE", adapter)
    row_id = seed_pending(
        recovery_env, "GOOGLE_DRIVE", None, quarantine_id="q-legacy-drive"
    )

    assert recovery_env.service.process_pending_deletions() == 1

    assert adapter.cleaned_ids == ["q-legacy-drive"]
    assert fetch_pending(recovery_env, row_id) is None


def test_legacy_local_restore_uses_registry(recovery_env):
    adapter = _FakeRecoveryAdapter("legacy-local")
    set_registry_adapter("LOCAL", adapter)
    row_id = seed_pending(
        recovery_env,
        "LOCAL",
        None,
        operation_type="RESTORE",
        quarantine_id="q-legacy-restore",
    )

    assert recovery_env.service.process_pending_deletions() == 1

    assert adapter.restored_ids == ["q-legacy-restore"]
    assert fetch_pending(recovery_env, row_id) is None


def test_legacy_drive_restore_uses_registry(recovery_env):
    adapter = _FakeRecoveryAdapter("legacy-drive")
    set_registry_adapter("GOOGLE_DRIVE", adapter)
    row_id = seed_pending(
        recovery_env,
        "GOOGLE_DRIVE",
        None,
        operation_type="RESTORE",
        quarantine_id="q-legacy-drive-restore",
    )

    assert recovery_env.service.process_pending_deletions() == 1

    assert adapter.restored_ids == ["q-legacy-drive-restore"]
    assert fetch_pending(recovery_env, row_id) is None


def test_legacy_missing_resource_cleanup_is_still_idempotent(recovery_env):
    adapter = _FakeRecoveryAdapter("legacy-local")
    adapter.cleanup_exc = StorageNotFoundError("already gone", provider="LOCAL")
    set_registry_adapter("LOCAL", adapter)
    row_id = seed_pending(recovery_env, "LOCAL", None, quarantine_id="q-gone")

    assert recovery_env.service.process_pending_deletions() == 1
    assert fetch_pending(recovery_env, row_id) is None


def test_legacy_null_ownership_survives_retry(recovery_env):
    adapter = _FakeRecoveryAdapter("legacy-local")
    adapter.cleanup_exc = StorageUnavailableError("down", provider="LOCAL")
    set_registry_adapter("LOCAL", adapter)
    row_id = seed_pending(recovery_env, "LOCAL", None)

    assert recovery_env.service.process_pending_deletions() == 0

    row = fetch_pending(recovery_env, row_id)
    assert row is not None
    assert row.provider_account_id is None
    assert row.status == "PENDING"
    assert row.retry_count == 1


# ---------------------------------------------------------------------------
# B. Managed LOCAL recovery with two roots
# ---------------------------------------------------------------------------


def test_managed_local_cleanup_isolated_between_roots(recovery_env, tmp_path):
    root_a = make_local_root(tmp_path, "root-a", "a.bin", b"root a")
    root_b = make_local_root(tmp_path, "root-b", "b.bin", b"root b")

    account_a = seed_account(
        recovery_env, "LOCAL", configuration={"storage_root": str(root_a)}
    )
    account_b = seed_account(
        recovery_env, "LOCAL", configuration={"storage_root": str(root_b)}
    )
    local_a = LocalAdapter(str(root_a))
    local_b = LocalAdapter(str(root_b))
    adapter_manager.install(account_a, local_a)
    adapter_manager.install(account_b, local_b)

    quarantine_a = local_a.quarantine("a.bin")
    quarantine_b = local_b.quarantine("b.bin")
    seed_pending(
        recovery_env,
        "LOCAL",
        account_a,
        provider_file_id="a.bin",
        quarantine_id=quarantine_a,
    )

    assert recovery_env.service.process_pending_deletions() == 1

    assert not (root_a / ".quarantine" / quarantine_a).exists()
    # Account B's quarantined file is untouched.
    assert (root_b / ".quarantine" / quarantine_b / "b.bin").read_bytes() == b"root b"

    row_id_b = seed_pending(
        recovery_env,
        "LOCAL",
        account_b,
        provider_file_id="b.bin",
        quarantine_id=quarantine_b,
    )
    assert recovery_env.service.process_pending_deletions() == 1
    assert fetch_pending(recovery_env, row_id_b) is None
    assert not (root_b / ".quarantine" / quarantine_b).exists()


def test_managed_local_restore_returns_file_to_own_root(recovery_env, tmp_path):
    root_a = make_local_root(tmp_path, "restore-a", "a.bin", b"restore me")
    root_b = make_local_root(tmp_path, "restore-b", "b.bin", b"leave me")

    account_a = seed_account(
        recovery_env, "LOCAL", configuration={"storage_root": str(root_a)}
    )
    account_b = seed_account(
        recovery_env, "LOCAL", configuration={"storage_root": str(root_b)}
    )
    local_a = LocalAdapter(str(root_a))
    local_b = LocalAdapter(str(root_b))
    adapter_manager.install(account_a, local_a)
    adapter_manager.install(account_b, local_b)

    quarantine_a = local_a.quarantine("a.bin")
    quarantine_b = local_b.quarantine("b.bin")

    row_id = seed_pending(
        recovery_env,
        "LOCAL",
        account_a,
        operation_type="RESTORE",
        provider_file_id="a.bin",
        quarantine_id=quarantine_a,
    )

    assert recovery_env.service.process_pending_deletions() == 1

    assert (root_a / "a.bin").read_bytes() == b"restore me"
    assert not (root_a / ".quarantine" / quarantine_a).exists()
    # Root B was never involved; its quarantined file is untouched.
    assert (root_b / ".quarantine" / quarantine_b / "b.bin").exists()
    assert not (root_b / "a.bin").exists()
    assert fetch_pending(recovery_env, row_id) is None


def test_managed_local_restore_does_not_use_other_account_adapter(
    recovery_env, tmp_path
):
    root_a = tmp_path / "wrong-root"
    root_b = tmp_path / "right-root"
    root_a.mkdir()
    root_b.mkdir()
    (root_b / "target.bin").write_bytes(b"target")

    account_a = seed_account(
        recovery_env, "LOCAL", configuration={"storage_root": str(root_a)}
    )
    account_b = seed_account(
        recovery_env, "LOCAL", configuration={"storage_root": str(root_b)}
    )
    local_a = LocalAdapter(str(root_a))
    local_b = LocalAdapter(str(root_b))
    adapter_manager.install(account_a, local_a)
    adapter_manager.install(account_b, local_b)

    quarantine_b = local_b.quarantine("target.bin")
    row_id = seed_pending(
        recovery_env,
        "LOCAL",
        account_b,
        operation_type="RESTORE",
        provider_file_id="target.bin",
        quarantine_id=quarantine_b,
    )

    assert recovery_env.service.process_pending_deletions() == 1

    assert (root_b / "target.bin").read_bytes() == b"target"
    assert not (root_a / "target.bin").exists()
    assert fetch_pending(recovery_env, row_id) is None


# ---------------------------------------------------------------------------
# C. Managed Google Drive (fake account adapters)
# ---------------------------------------------------------------------------


def test_managed_drive_cleanup_uses_exact_account(recovery_env):
    fake_a = _FakeRecoveryAdapter("acct-a")
    fake_b = _FakeRecoveryAdapter("acct-b")
    account_a = seed_account(recovery_env, "GOOGLE_DRIVE")
    account_b = seed_account(recovery_env, "GOOGLE_DRIVE")
    adapter_manager.install(account_a, fake_a)
    adapter_manager.install(account_b, fake_b)
    legacy = _ExplodingRegistryAdapter()
    set_registry_adapter("GOOGLE_DRIVE", legacy)

    seed_pending(recovery_env, "GOOGLE_DRIVE", account_a, quarantine_id="q-a")
    seed_pending(recovery_env, "GOOGLE_DRIVE", account_b, quarantine_id="q-b")

    assert recovery_env.service.process_pending_deletions() == 2

    assert fake_a.cleaned_ids == ["q-a"]
    assert fake_b.cleaned_ids == ["q-b"]
    assert legacy.calls == []
    assert all_pending(recovery_env) == []


def test_managed_drive_restore_uses_exact_account(recovery_env):
    fake_a = _FakeRecoveryAdapter("acct-a")
    fake_b = _FakeRecoveryAdapter("acct-b")
    account_a = seed_account(recovery_env, "GOOGLE_DRIVE")
    account_b = seed_account(recovery_env, "GOOGLE_DRIVE")
    adapter_manager.install(account_a, fake_a)
    adapter_manager.install(account_b, fake_b)
    legacy = _ExplodingRegistryAdapter()
    set_registry_adapter("GOOGLE_DRIVE", legacy)

    seed_pending(
        recovery_env,
        "GOOGLE_DRIVE",
        account_a,
        operation_type="RESTORE",
        quarantine_id="r-a",
    )
    seed_pending(
        recovery_env,
        "GOOGLE_DRIVE",
        account_b,
        operation_type="RESTORE",
        quarantine_id="r-b",
    )

    assert recovery_env.service.process_pending_deletions() == 2

    assert fake_a.restored_ids == ["r-a"]
    assert fake_b.restored_ids == ["r-b"]
    assert legacy.calls == []
    assert all_pending(recovery_env) == []


# ---------------------------------------------------------------------------
# D. Fail-closed account errors
# ---------------------------------------------------------------------------


def _assert_retained_with_ownership(env, row_id, account_id):
    row = fetch_pending(env, row_id)
    assert row is not None
    assert row.provider_account_id == account_id
    assert row.status in ("PENDING", "FAILED")
    assert row.retry_count >= 1
    return row


def test_unknown_account_never_cleans_up(recovery_env):
    legacy = _ExplodingRegistryAdapter()
    set_registry_adapter("GOOGLE_DRIVE", legacy)
    missing = str(uuid.uuid4())
    row_id = seed_pending(recovery_env, "GOOGLE_DRIVE", missing)

    assert recovery_env.service.process_pending_deletions() == 0

    row = _assert_retained_with_ownership(recovery_env, row_id, missing)
    assert "not found" in (row.last_error or "").lower()
    assert legacy.calls == []


def test_disconnected_account_never_cleans_up(recovery_env):
    fake = _FakeRecoveryAdapter("disconnected")
    account_id = seed_account(recovery_env, "GOOGLE_DRIVE", status="disconnected")
    adapter_manager.install(account_id, fake)
    legacy = _ExplodingRegistryAdapter()
    set_registry_adapter("GOOGLE_DRIVE", legacy)
    row_id = seed_pending(recovery_env, "GOOGLE_DRIVE", account_id)

    assert recovery_env.service.process_pending_deletions() == 0

    _assert_retained_with_ownership(recovery_env, row_id, account_id)
    assert fake.cleaned_ids == []
    assert legacy.calls == []


def test_provider_mismatch_never_cleans_up(recovery_env):
    fake = _FakeRecoveryAdapter("mismatch")
    account_id = seed_account(recovery_env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)
    legacy = _ExplodingRegistryAdapter()
    set_registry_adapter("LOCAL", legacy)
    row_id = seed_pending(recovery_env, "LOCAL", account_id)

    assert recovery_env.service.process_pending_deletions() == 0

    row = _assert_retained_with_ownership(recovery_env, row_id, account_id)
    assert "not" in (row.last_error or "").lower()
    assert fake.cleaned_ids == []
    assert legacy.calls == []


def test_storage_unavailable_during_resolution_never_cleans_up(
    recovery_env, monkeypatch
):
    account_id = seed_account(recovery_env, "GOOGLE_DRIVE")
    row_id = seed_pending(recovery_env, "GOOGLE_DRIVE", account_id)

    def unavailable(_account_id):
        raise StorageUnavailableError(
            detail="credential store unavailable", provider="GOOGLE_DRIVE"
        )

    monkeypatch.setattr(adapter_manager, "resolve", unavailable)

    assert recovery_env.service.process_pending_deletions() == 0

    row = _assert_retained_with_ownership(recovery_env, row_id, account_id)
    assert "unavailable" in (row.last_error or "").lower()


def test_generic_resolution_failure_never_cleans_up(recovery_env, monkeypatch):
    account_id = seed_account(recovery_env, "GOOGLE_DRIVE")
    row_id = seed_pending(recovery_env, "GOOGLE_DRIVE", account_id)

    def boom(_account_id):
        raise RuntimeError("unexpected resolver failure")

    monkeypatch.setattr(adapter_manager, "resolve", boom)

    assert recovery_env.service.process_pending_deletions() == 0

    row = _assert_retained_with_ownership(recovery_env, row_id, account_id)
    assert "unexpected resolver failure" in (row.last_error or "")


def test_account_removed_during_recovery_never_cleans_up(recovery_env):
    fake = _FakeRecoveryAdapter("gone-account")
    account_id = seed_account(recovery_env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)
    row_id = seed_pending(recovery_env, "GOOGLE_DRIVE", account_id)

    delete_account(recovery_env, account_id)

    assert recovery_env.service.process_pending_deletions() == 0

    _assert_retained_with_ownership(recovery_env, row_id, account_id)
    assert fake.cleaned_ids == []


def test_managed_restore_resolution_failure_retains_record(recovery_env):
    legacy = _ExplodingRegistryAdapter()
    set_registry_adapter("GOOGLE_DRIVE", legacy)
    missing = str(uuid.uuid4())
    row_id = seed_pending(
        recovery_env,
        "GOOGLE_DRIVE",
        missing,
        operation_type="RESTORE",
        quarantine_id="r-missing",
    )

    assert recovery_env.service.process_pending_deletions() == 0

    row = _assert_retained_with_ownership(recovery_env, row_id, missing)
    assert row.operation_type == "RESTORE"
    assert legacy.calls == []


# ---------------------------------------------------------------------------
# E. Missing-file distinction
# ---------------------------------------------------------------------------


def test_missing_resource_in_correct_account_is_idempotent_cleanup(recovery_env):
    fake = _FakeRecoveryAdapter("acct-a")
    fake.cleanup_exc = StorageNotFoundError("already gone", provider="GOOGLE_DRIVE")
    account_id = seed_account(recovery_env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)
    row_id = seed_pending(recovery_env, "GOOGLE_DRIVE", account_id)

    assert recovery_env.service.process_pending_deletions() == 1
    assert fetch_pending(recovery_env, row_id) is None


def test_missing_resource_in_unknown_account_is_not_success(recovery_env):
    missing = str(uuid.uuid4())
    row_id = seed_pending(recovery_env, "GOOGLE_DRIVE", missing)

    assert recovery_env.service.process_pending_deletions() == 0

    row = _assert_retained_with_ownership(recovery_env, row_id, missing)
    # The account error is recorded, never "resource absent".
    assert "account" in (row.last_error or "").lower()


def test_missing_resource_in_disconnected_account_is_not_success(recovery_env):
    fake = _FakeRecoveryAdapter("disconnected")
    fake.cleanup_exc = StorageNotFoundError("absent", provider="GOOGLE_DRIVE")
    account_id = seed_account(recovery_env, "GOOGLE_DRIVE", status="disconnected")
    adapter_manager.install(account_id, fake)
    row_id = seed_pending(recovery_env, "GOOGLE_DRIVE", account_id)

    assert recovery_env.service.process_pending_deletions() == 0

    _assert_retained_with_ownership(recovery_env, row_id, account_id)
    assert fake.cleaned_ids == []


def test_missing_resource_in_mismatched_account_is_not_success(recovery_env):
    fake = _FakeRecoveryAdapter("mismatch")
    fake.cleanup_exc = StorageNotFoundError("absent", provider="LOCAL")
    account_id = seed_account(recovery_env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)
    row_id = seed_pending(recovery_env, "LOCAL", account_id)

    assert recovery_env.service.process_pending_deletions() == 0

    _assert_retained_with_ownership(recovery_env, row_id, account_id)
    assert fake.cleaned_ids == []


# ---------------------------------------------------------------------------
# F. Concurrency, leases, and retry
# ---------------------------------------------------------------------------


def test_live_claim_is_not_reprocessed(recovery_env):
    adapter = _FakeRecoveryAdapter("legacy-local")
    set_registry_adapter("LOCAL", adapter)
    row_id = seed_pending(
        recovery_env,
        "LOCAL",
        None,
        status="PROCESSING",
        claim_token="active-token",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    assert recovery_env.service.process_pending_deletions() == 0

    row = fetch_pending(recovery_env, row_id)
    assert row.status == "PROCESSING"
    assert row.claim_token == "active-token"
    assert row.retry_count == 0
    assert adapter.cleaned_ids == []


def test_expired_lease_is_reclaimed_and_uses_account(recovery_env):
    fake = _FakeRecoveryAdapter("acct-a")
    account_id = seed_account(recovery_env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)
    row_id = seed_pending(
        recovery_env,
        "GOOGLE_DRIVE",
        account_id,
        status="PROCESSING",
        claim_token="stale-token",
        retry_count=1,
        lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    assert recovery_env.service.process_pending_deletions() == 1

    assert fake.cleaned_ids
    assert fetch_pending(recovery_env, row_id) is None


def test_retry_preserves_ownership_then_succeeds(recovery_env):
    fake = _FakeRecoveryAdapter("acct-a")
    fake.cleanup_exc = StorageUnavailableError("temporary", provider="GOOGLE_DRIVE")
    account_id = seed_account(recovery_env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)
    row_id = seed_pending(recovery_env, "GOOGLE_DRIVE", account_id)

    assert recovery_env.service.process_pending_deletions() == 0
    row = _assert_retained_with_ownership(recovery_env, row_id, account_id)
    assert row.next_retry_at is not None
    assert row.claim_token is None
    assert row.lease_expires_at is None

    # Clear the backoff and let the next attempt succeed.
    session = recovery_env.Session()
    try:
        fresh = session.get(PendingDeletion, row_id)
        fresh.next_retry_at = None
        session.commit()
    finally:
        session.close()

    fake.cleanup_exc = None
    assert recovery_env.service.process_pending_deletions() == 1
    assert fake.cleaned_ids == ["q-1"]
    assert fetch_pending(recovery_env, row_id) is None


def test_failed_restore_retains_ownership_and_error(recovery_env):
    fake = _FakeRecoveryAdapter("acct-a")
    fake.restore_exc = StorageUnavailableError("restore boom", provider="GOOGLE_DRIVE")
    account_id = seed_account(recovery_env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)
    row_id = seed_pending(
        recovery_env,
        "GOOGLE_DRIVE",
        account_id,
        operation_type="RESTORE",
        quarantine_id="r-fail",
    )

    assert recovery_env.service.process_pending_deletions() == 0

    row = _assert_retained_with_ownership(recovery_env, row_id, account_id)
    assert row.operation_type == "RESTORE"
    assert "restore boom" in (row.last_error or "")
    assert row.quarantine_id == "r-fail"


def test_commit_failure_during_finalize_keeps_recovery_record(
    recovery_env, monkeypatch
):
    fake = _FakeRecoveryAdapter("acct-a")
    account_id = seed_account(recovery_env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)
    row_id = seed_pending(recovery_env, "GOOGLE_DRIVE", account_id)

    real_commit = OrmSession.commit
    state = {"n": 0}

    def commit(session):
        state["n"] += 1
        if state["n"] == 2:  # 1 = claim commit, 2 = finalize commit
            raise RuntimeError("finalize commit failed")
        return real_commit(session)

    monkeypatch.setattr(OrmSession, "commit", commit)

    assert recovery_env.service.process_pending_deletions() == 0
    monkeypatch.setattr(OrmSession, "commit", real_commit)

    row = fetch_pending(recovery_env, row_id)
    assert row is not None
    assert row.provider_account_id == account_id
    assert row.status == "PROCESSING"  # lease remains for a later reclaim
    assert fake.cleaned_ids == ["q-1"]


def test_each_managed_row_is_finalized_with_its_own_quarantine(recovery_env):
    fake = _FakeRecoveryAdapter("acct-a")
    account_id = seed_account(recovery_env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)
    seed_pending(
        recovery_env,
        "GOOGLE_DRIVE",
        account_id,
        provider_file_id="file-1",
        quarantine_id="q-1",
    )
    seed_pending(
        recovery_env,
        "GOOGLE_DRIVE",
        account_id,
        provider_file_id="file-2",
        quarantine_id="q-2",
    )

    assert recovery_env.service.process_pending_deletions() == 2

    assert set(fake.cleaned_ids) == {"q-1", "q-2"}
    assert all_pending(recovery_env) == []
