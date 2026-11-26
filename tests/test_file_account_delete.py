"""Account-aware delete and quarantine tests (HVSA H1.5.4 Step 3B).

Verifies that ``DELETE /api/v1/files/{file_id}``:

* keeps legacy NULL-account records on the provider registry,
* resolves managed records through their exact account adapter for
  quarantine, rollback restoration, and cleanup,
* fails closed on unknown, disconnected, provider-mismatched, or
  unavailable managed accounts without ever consulting the registry,
* copies the file's account ownership onto PendingDeletion (NULL preserved),
* restores through the same resolved adapter when the DB commit fails,
* preserves the existing PROCESSING claim / lease / retry protocol.

All tests use isolated databases, fake or temporary-directory adapters, and
no credentials, keyring, or provider APIs.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.v1.endpoints.account_adapter_http import STORAGE_UNAVAILABLE_DETAIL
from backend.config import settings
from backend.database import Base, get_db
from backend.main import app
from backend.models.pending_deletion import PendingDeletion
from backend.models.storage_account import StorageAccount
from backend.models.unified_file import UnifiedFileRecord
from backend.storage.account_adapter_manager import adapter_manager
from backend.storage.capabilities import CAPABILITY_MAP
from backend.storage.errors import StorageNotFoundError, StorageUnavailableError
from backend.storage.interfaces import StorageOperations
from backend.storage.local import LocalAdapter
from backend.storage.registry import _adapter_map, _capability_map, register
from backend.storage.types import ProviderFileRecord


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeDeleteAdapter(StorageOperations):
    """Account-scoped fake adapter recording quarantine/restore/cleanup calls."""

    def __init__(self, tag: str, *, quarantine_id: str | None = None):
        self.tag = tag
        self._quarantine_id = quarantine_id
        self.quarantine_calls: list[str] = []
        self.restore_calls: list[str] = []
        self.cleanup_calls: list[str] = []
        self.quarantine_error: Exception | None = None
        self.restore_error: Exception | None = None
        self.cleanup_error: Exception | None = None
        self.on_cleanup = None

    # Unused operations -------------------------------------------------

    def upload(self, file_data, name, logical_path):
        raise NotImplementedError

    def download(self, provider_file_id: str) -> bytes:
        raise NotImplementedError

    def rename(self, provider_file_id: str, new_name: str) -> ProviderFileRecord:
        raise NotImplementedError

    def move(
        self, source_provider_file_id: str, target_logical_path: str
    ) -> ProviderFileRecord:
        raise NotImplementedError

    # Deletion protocol -------------------------------------------------

    def quarantine(self, provider_file_id: str) -> str:
        self.quarantine_calls.append(provider_file_id)
        if self.quarantine_error is not None:
            raise self.quarantine_error
        if self._quarantine_id is not None:
            return self._quarantine_id
        return f"q::{self.tag}::{provider_file_id}"

    def restore(self, quarantine_id: str) -> ProviderFileRecord:
        self.restore_calls.append(quarantine_id)
        if self.restore_error is not None:
            raise self.restore_error
        return ProviderFileRecord(
            provider_file_id="restored",
            name="restored",
            logical_path="/restored",
            mime_type="application/octet-stream",
            size_bytes=0,
        )

    def cleanup(self, quarantine_id: str) -> None:
        self.cleanup_calls.append(quarantine_id)
        if self.on_cleanup is not None:
            self.on_cleanup(quarantine_id)
        if self.cleanup_error is not None:
            raise self.cleanup_error


class _ExplodingAdapter(StorageOperations):
    """Legacy registry adapter that fails the test if it is ever used."""

    def __init__(self, provider: str):
        self.provider = provider

    def upload(self, file_data, name, logical_path):
        raise NotImplementedError

    def download(self, provider_file_id: str) -> bytes:
        raise NotImplementedError

    def rename(self, provider_file_id: str, new_name: str) -> ProviderFileRecord:
        raise NotImplementedError

    def move(
        self, source_provider_file_id: str, target_logical_path: str
    ) -> ProviderFileRecord:
        raise NotImplementedError

    def quarantine(self, provider_file_id: str) -> str:
        raise AssertionError(
            f"legacy registry must not be used for a managed {self.provider} record"
        )

    def restore(self, quarantine_id: str):
        raise AssertionError(
            f"legacy registry must not restore a managed {self.provider} record"
        )

    def cleanup(self, quarantine_id: str) -> None:
        raise AssertionError(
            f"legacy registry must not clean up a managed {self.provider} record"
        )


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def _build_env(tmp_path, monkeypatch, engine) -> SimpleNamespace:
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
    monkeypatch.setattr(settings, "uploads_dir", str(uploads_dir))

    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db

    legacy_local = LocalAdapter(str(uploads_dir))
    _adapter_map.pop("LOCAL", None)
    _capability_map.pop("LOCAL", None)
    register("LOCAL", legacy_local, CAPABILITY_MAP["LOCAL"])

    monkeypatch.setattr(adapter_manager, "_session_factory", TestSession)
    adapter_manager.invalidate_all()

    return SimpleNamespace(
        client=TestClient(app, raise_server_exceptions=False),
        engine=engine,
        Session=TestSession,
        uploads=uploads_dir,
        legacy_local=legacy_local,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    test_env = _build_env(tmp_path, monkeypatch, engine)
    try:
        yield test_env
    finally:
        adapter_manager.invalidate_all()
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.fixture
def file_env(tmp_path, monkeypatch):
    """File-backed database so the heartbeat thread uses a separate connection."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'delete.db'}",
        connect_args={"check_same_thread": False},
    )
    test_env = _build_env(tmp_path, monkeypatch, engine)
    try:
        yield test_env
    finally:
        adapter_manager.invalidate_all()
        app.dependency_overrides.clear()
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


def seed_record(
    env,
    provider: str = "LOCAL",
    account_id: str | None = None,
    provider_file_id: str = "stored.bin",
    name: str = "file.bin",
    logical_path: str = "/file.bin",
    mime_type: str = "text/plain",
) -> str:
    session = env.Session()
    try:
        record = UnifiedFileRecord(
            provider=provider,
            provider_account_id=account_id,
            provider_file_id=provider_file_id,
            name=name,
            logical_path=logical_path,
            mime_type=mime_type,
            size_bytes=10,
            sha256="ab" * 32,
            category="Documents",
            tags=["one", "two"],
            sync_state="LOCAL" if provider == "LOCAL" else "COMPLETED",
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record.id
    finally:
        session.close()


def fetch_record(env, file_id: str):
    session = env.Session()
    try:
        return session.get(UnifiedFileRecord, file_id)
    finally:
        session.close()


def pending_rows(env) -> list[PendingDeletion]:
    session = env.Session()
    try:
        return list(session.execute(select(PendingDeletion)).scalars().all())
    finally:
        session.close()


def do_delete(env, file_id: str):
    return env.client.delete(f"/api/v1/files/{file_id}")


def fail_first_commit(monkeypatch) -> dict:
    """Fail the first ORM commit (the delete transaction), allow later ones."""
    real_commit = OrmSession.commit
    calls = {"n": 0}

    def commit(session):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db commit failed")
        return real_commit(session)

    monkeypatch.setattr(OrmSession, "commit", commit)
    return calls


# ---------------------------------------------------------------------------
# A. Legacy compatibility
# ---------------------------------------------------------------------------


def test_legacy_local_delete_still_works(env):
    (env.uploads / "legacy.bin").write_bytes(b"legacy payload")
    file_id = seed_record(env, "LOCAL", None, "legacy.bin")

    response = do_delete(env, file_id)

    assert response.status_code == 204
    assert response.content == b""
    assert not (env.uploads / "legacy.bin").exists()
    assert fetch_record(env, file_id) is None
    assert pending_rows(env) == []

    activity = env.client.get("/api/v1/activity").json()
    assert activity["total"] == 1
    assert activity["items"][0]["action_type"] == "DELETE"
    assert activity["items"][0]["file_name"] == "file.bin"


def test_legacy_local_pending_deletion_keeps_null_account(env, monkeypatch):
    (env.uploads / "legacy-pending.bin").write_bytes(b"payload")
    file_id = seed_record(env, "LOCAL", None, "legacy-pending.bin")

    monkeypatch.setattr(
        env.legacy_local,
        "cleanup",
        lambda qid: (_ for _ in ()).throw(
            StorageUnavailableError("cleanup failed", provider="LOCAL")
        ),
    )

    response = do_delete(env, file_id)

    assert response.status_code == 204
    rows = pending_rows(env)
    assert len(rows) == 1
    row = rows[0]
    assert row.provider == "LOCAL"
    assert row.provider_account_id is None
    assert row.provider_file_id == "legacy-pending.bin"
    assert row.operation_type == "CLEANUP"
    assert row.status in ("PENDING", "FAILED")
    assert row.next_retry_at is not None


def test_legacy_drive_delete_uses_provider_registry(env):
    fake = _FakeDeleteAdapter("legacy-drive")
    set_registry_adapter("GOOGLE_DRIVE", fake)
    file_id = seed_record(env, "GOOGLE_DRIVE", None, "drive-legacy-1")

    response = do_delete(env, file_id)

    assert response.status_code == 204
    assert fake.quarantine_calls == ["drive-legacy-1"]
    assert fake.cleanup_calls == ["q::legacy-drive::drive-legacy-1"]
    assert fetch_record(env, file_id) is None
    assert pending_rows(env) == []


def test_legacy_drive_pending_deletion_keeps_null_account(env):
    fake = _FakeDeleteAdapter("legacy-drive")
    fake.cleanup_error = StorageUnavailableError(
        "cleanup failed", provider="GOOGLE_DRIVE"
    )
    set_registry_adapter("GOOGLE_DRIVE", fake)
    file_id = seed_record(env, "GOOGLE_DRIVE", None, "drive-legacy-2")

    response = do_delete(env, file_id)

    assert response.status_code == 204
    rows = pending_rows(env)
    assert len(rows) == 1
    assert rows[0].provider_account_id is None
    assert rows[0].provider == "GOOGLE_DRIVE"
    assert rows[0].quarantine_id == "q::legacy-drive::drive-legacy-2"
    assert rows[0].operation_type == "CLEANUP"


def test_legacy_unregistered_provider_returns_503(env):
    file_id = seed_record(env, "BOX", None, "box-file-1")

    response = do_delete(env, file_id)

    assert response.status_code == 503
    assert fetch_record(env, file_id) is not None
    assert pending_rows(env) == []


# ---------------------------------------------------------------------------
# B. Managed-account deletion
# ---------------------------------------------------------------------------


def test_managed_local_delete_uses_exact_account_adapter(env, tmp_path):
    root = tmp_path / "account-root"
    root.mkdir()
    (root / "a.bin").write_bytes(b"account payload")

    account_id = seed_account(env, "LOCAL")
    adapter_manager.install(account_id, LocalAdapter(str(root)))
    set_registry_adapter("LOCAL", _ExplodingAdapter("LOCAL"))

    file_id = seed_record(env, "LOCAL", account_id, "a.bin")

    response = do_delete(env, file_id)

    assert response.status_code == 204
    assert not (root / "a.bin").exists()
    assert (root / ".quarantine").exists()
    assert fetch_record(env, file_id) is None
    assert pending_rows(env) == []


def test_managed_drive_delete_uses_exact_account_adapter(env):
    fake = _FakeDeleteAdapter("account-a")
    account_id = seed_account(env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)
    set_registry_adapter("GOOGLE_DRIVE", _ExplodingAdapter("GOOGLE_DRIVE"))

    file_id = seed_record(env, "GOOGLE_DRIVE", account_id, "drive-a")

    response = do_delete(env, file_id)

    assert response.status_code == 204
    assert fake.quarantine_calls == ["drive-a"]
    assert fake.cleanup_calls == ["q::account-a::drive-a"]
    assert fake.restore_calls == []
    assert fetch_record(env, file_id) is None


def test_managed_delete_pending_deletion_records_account_id(env):
    fake = _FakeDeleteAdapter("account-a")
    fake.cleanup_error = StorageUnavailableError(
        "cleanup failed", provider="GOOGLE_DRIVE"
    )
    account_id = seed_account(env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)

    file_id = seed_record(env, "GOOGLE_DRIVE", account_id, "drive-pending")

    response = do_delete(env, file_id)

    assert response.status_code == 204
    rows = pending_rows(env)
    assert len(rows) == 1
    row = rows[0]
    assert row.provider_account_id == account_id
    assert row.provider == "GOOGLE_DRIVE"
    assert row.provider_file_id == "drive-pending"
    assert row.quarantine_id == "q::account-a::drive-pending"
    assert row.operation_type == "CLEANUP"
    assert row.file_name_snapshot == "file.bin"
    assert row.status in ("PENDING", "FAILED")
    assert row.next_retry_at is not None


def test_managed_local_pending_deletion_records_account_id(env, tmp_path):
    root = tmp_path / "root-managed"
    root.mkdir()
    (root / "m.bin").write_bytes(b"managed")

    account_id = seed_account(env, "LOCAL")
    adapter = LocalAdapter(str(root))
    adapter_manager.install(account_id, adapter)
    file_id = seed_record(env, "LOCAL", account_id, "m.bin")

    # Force cleanup to fail by replacing the quarantine entry with a file.
    original_cleanup = adapter.cleanup

    def failing_cleanup(quarantine_id):
        raise StorageUnavailableError("cleanup failed", provider="LOCAL")

    adapter.cleanup = failing_cleanup
    try:
        response = do_delete(env, file_id)
    finally:
        adapter.cleanup = original_cleanup

    assert response.status_code == 204, response.text
    rows = pending_rows(env)
    assert len(rows) == 1
    assert rows[0].provider_account_id == account_id
    assert rows[0].quarantine_id


# ---------------------------------------------------------------------------
# C. Multi-account isolation
# ---------------------------------------------------------------------------


def test_two_drive_accounts_are_isolated(env):
    fake_a = _FakeDeleteAdapter("acct-a")
    fake_b = _FakeDeleteAdapter("acct-b")
    account_a = seed_account(env, "GOOGLE_DRIVE")
    account_b = seed_account(env, "GOOGLE_DRIVE")
    adapter_manager.install(account_a, fake_a)
    adapter_manager.install(account_b, fake_b)

    file_a = seed_record(env, "GOOGLE_DRIVE", account_a, "drive-a")
    file_b = seed_record(env, "GOOGLE_DRIVE", account_b, "drive-b")

    assert do_delete(env, file_a).status_code == 204
    assert fake_a.quarantine_calls == ["drive-a"]
    assert fake_a.cleanup_calls == ["q::acct-a::drive-a"]
    assert fake_b.quarantine_calls == []
    assert fake_b.cleanup_calls == []
    assert fetch_record(env, file_b) is not None

    assert do_delete(env, file_b).status_code == 204
    assert fake_b.quarantine_calls == ["drive-b"]
    assert fake_b.cleanup_calls == ["q::acct-b::drive-b"]
    assert fake_a.quarantine_calls == ["drive-a"]


def test_two_local_roots_stay_isolated(env, tmp_path):
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "a.bin").write_bytes(b"root a")
    (root_b / "b.bin").write_bytes(b"root b")

    account_a = seed_account(env, "LOCAL", configuration={"storage_root": str(root_a)})
    account_b = seed_account(env, "LOCAL", configuration={"storage_root": str(root_b)})
    adapter_manager.install(account_a, LocalAdapter(str(root_a)))
    adapter_manager.install(account_b, LocalAdapter(str(root_b)))

    file_a = seed_record(env, "LOCAL", account_a, "a.bin")
    file_b = seed_record(env, "LOCAL", account_b, "b.bin")

    assert do_delete(env, file_a).status_code == 204
    assert not (root_a / "a.bin").exists()
    assert (root_b / "b.bin").read_bytes() == b"root b"
    assert not (root_b / ".quarantine").exists()

    assert do_delete(env, file_b).status_code == 204
    assert not (root_b / "b.bin").exists()


def test_invalidating_one_account_does_not_affect_another(env):
    fake_a = _FakeDeleteAdapter("acct-a")
    fake_b = _FakeDeleteAdapter("acct-b")
    account_a = seed_account(env, "GOOGLE_DRIVE")
    account_b = seed_account(env, "GOOGLE_DRIVE")
    adapter_manager.install(account_a, fake_a)
    adapter_manager.install(account_b, fake_b)

    adapter_manager.invalidate_account(account_a)

    assert adapter_manager.cache.get(account_a) is None
    assert adapter_manager.cache.get(account_b) is fake_b

    file_b = seed_record(env, "GOOGLE_DRIVE", account_b, "drive-b")
    assert do_delete(env, file_b).status_code == 204

    assert fake_b.quarantine_calls == ["drive-b"]
    assert fake_a.quarantine_calls == []


# ---------------------------------------------------------------------------
# D. Fail-closed behavior
# ---------------------------------------------------------------------------


def test_unknown_account_returns_404_without_quarantine(env):
    set_registry_adapter("GOOGLE_DRIVE", _ExplodingAdapter("GOOGLE_DRIVE"))
    missing_account = str(uuid.uuid4())
    file_id = seed_record(env, "GOOGLE_DRIVE", missing_account, "orphan")

    response = do_delete(env, file_id)

    assert response.status_code == 404
    assert "storage account" in response.json()["detail"].lower()
    assert missing_account not in response.text
    assert fetch_record(env, file_id) is not None
    assert pending_rows(env) == []


def test_disconnected_account_returns_503_without_quarantine(env):
    fake = _FakeDeleteAdapter("disconnected")
    account_id = seed_account(env, "GOOGLE_DRIVE", status="disconnected")
    adapter_manager.install(account_id, fake)
    file_id = seed_record(env, "GOOGLE_DRIVE", account_id, "drive-x")

    response = do_delete(env, file_id)

    assert response.status_code == 503
    assert fake.quarantine_calls == []
    assert fake.cleanup_calls == []
    assert fetch_record(env, file_id) is not None
    assert pending_rows(env) == []


def test_provider_mismatch_returns_409_without_quarantine(env):
    fake = _FakeDeleteAdapter("mismatch")
    account_id = seed_account(env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)
    file_id = seed_record(env, "LOCAL", account_id, "local-claimed.bin")

    response = do_delete(env, file_id)

    assert response.status_code == 409
    assert fake.quarantine_calls == []
    assert fetch_record(env, file_id) is not None
    assert pending_rows(env) == []


def test_storage_unavailable_during_resolution_returns_503(env, monkeypatch):
    account_id = seed_account(env, "GOOGLE_DRIVE")
    file_id = seed_record(env, "GOOGLE_DRIVE", account_id, "drive-u")

    def unavailable(_account_id):
        raise StorageUnavailableError(
            detail="credential store unavailable", provider="GOOGLE_DRIVE"
        )

    monkeypatch.setattr(adapter_manager, "resolve", unavailable)

    response = do_delete(env, file_id)

    assert response.status_code == 503
    # Fixed public message: the internal resolution detail is never echoed.
    assert response.json()["detail"] == STORAGE_UNAVAILABLE_DETAIL
    assert "credential store" not in response.text.lower()
    assert account_id not in response.text
    assert fetch_record(env, file_id) is not None
    assert pending_rows(env) == []


def test_managed_quarantine_provider_error_is_preserved(env):
    fake = _FakeDeleteAdapter("acct")
    fake.quarantine_error = StorageNotFoundError("gone", provider="GOOGLE_DRIVE")
    account_id = seed_account(env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)
    file_id = seed_record(env, "GOOGLE_DRIVE", account_id, "missing-drive")

    response = do_delete(env, file_id)

    assert response.status_code == 404
    assert fetch_record(env, file_id) is not None
    assert pending_rows(env) == []


def test_managed_resolution_never_falls_back_to_registry(env):
    legacy = _ExplodingAdapter("GOOGLE_DRIVE")
    set_registry_adapter("GOOGLE_DRIVE", legacy)
    file_id = seed_record(env, "GOOGLE_DRIVE", str(uuid.uuid4()), "no-account")

    response = do_delete(env, file_id)

    # The exploding legacy adapter would raise AssertionError (HTTP 500).
    assert response.status_code == 404
    assert fetch_record(env, file_id) is not None


# ---------------------------------------------------------------------------
# E. Rollback behavior
# ---------------------------------------------------------------------------


def test_managed_db_commit_failure_restores_with_same_adapter(env, monkeypatch):
    fake = _FakeDeleteAdapter("acct-a")
    account_id = seed_account(env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)
    set_registry_adapter("GOOGLE_DRIVE", _ExplodingAdapter("GOOGLE_DRIVE"))
    file_id = seed_record(env, "GOOGLE_DRIVE", account_id, "drive-rollback")

    fail_first_commit(monkeypatch)

    response = do_delete(env, file_id)

    assert response.status_code == 500
    assert "restored" in response.json()["detail"].lower()
    assert fake.quarantine_calls == ["drive-rollback"]
    assert fake.restore_calls == ["q::acct-a::drive-rollback"]
    assert fake.cleanup_calls == []

    record = fetch_record(env, file_id)
    assert record is not None
    assert record.provider_account_id == account_id

    # A successful immediate restore needs no recovery row; the failed-restore
    # path (which does queue one) is covered separately below.
    assert pending_rows(env) == []

    # No DELETE activity for a failed deletion.
    assert env.client.get("/api/v1/activity").json()["total"] == 0


def test_legacy_db_commit_failure_restores_physical_file(env, monkeypatch):
    (env.uploads / "legacy-rollback.bin").write_bytes(b"original bytes")
    file_id = seed_record(env, "LOCAL", None, "legacy-rollback.bin")

    fail_first_commit(monkeypatch)

    response = do_delete(env, file_id)

    assert response.status_code == 500
    assert (env.uploads / "legacy-rollback.bin").read_bytes() == b"original bytes"
    assert fetch_record(env, file_id) is not None
    assert pending_rows(env) == []


def test_managed_restore_failure_queues_recovery_and_logs(env, monkeypatch, caplog):
    fake = _FakeDeleteAdapter("acct-a")
    fake.restore_error = StorageUnavailableError(
        "restore failed", provider="GOOGLE_DRIVE"
    )
    account_id = seed_account(env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)
    file_id = seed_record(env, "GOOGLE_DRIVE", account_id, "drive-restore-fail")

    fail_first_commit(monkeypatch)
    caplog.set_level(logging.ERROR, logger="backend.api.v1.endpoints.files")

    response = do_delete(env, file_id)

    assert response.status_code == 500
    assert "recovery retry has been queued" in response.json()["detail"].lower()
    # Internal logs keep the real restore failure; the API body does not.
    assert any(
        "failed to restore quarantined file" in record.getMessage()
        and "restore failed" in record.getMessage()
        for record in caplog.records
    )
    assert "restore failed" not in response.text

    rows = pending_rows(env)
    assert len(rows) == 1
    assert rows[0].operation_type == "RESTORE"
    assert rows[0].provider_account_id == account_id
    assert "restore failed" in (rows[0].last_error or "").lower()
    assert fetch_record(env, file_id) is not None


def test_managed_double_commit_failure_reports_manual_recovery(
    env, monkeypatch, caplog
):
    fake = _FakeDeleteAdapter("acct-a")
    fake.restore_error = StorageUnavailableError(
        "restore failed", provider="GOOGLE_DRIVE"
    )
    account_id = seed_account(env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)
    file_id = seed_record(env, "GOOGLE_DRIVE", account_id, "drive-double-fail")

    monkeypatch.setattr(
        OrmSession, "commit", lambda session: (_ for _ in ()).throw(
            RuntimeError("db commit failed")
        )
    )
    caplog.set_level(logging.ERROR, logger="backend.api.v1.endpoints.files")

    response = do_delete(env, file_id)

    assert response.status_code == 500
    assert "manual recovery may be required" in response.json()["detail"].lower()
    assert any(
        "failed to persist restore recovery record" in record.getMessage().lower()
        for record in caplog.records
    )
    assert "db commit failed" not in response.text
    assert "restore failed" not in response.text
    assert pending_rows(env) == []
    assert fetch_record(env, file_id) is not None


# ---------------------------------------------------------------------------
# F. Concurrency and claim preservation
# ---------------------------------------------------------------------------


def test_managed_delete_holds_processing_claim_during_cleanup(file_env):
    fake = _FakeDeleteAdapter("acct-a")
    account_id = seed_account(file_env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)
    file_id = seed_record(file_env, "GOOGLE_DRIVE", account_id, "drive-claim")

    observed: dict = {}

    def inspect_pending(_quarantine_id):
        session = file_env.Session()
        try:
            row = session.execute(select(PendingDeletion)).scalar_one()
            observed.update(
                status=row.status,
                claim_token=row.claim_token,
                processing_started_at=row.processing_started_at,
                lease_expires_at=row.lease_expires_at,
                operation_generation=row.operation_generation,
                provider_account_id=row.provider_account_id,
            )
        finally:
            session.close()

    fake.on_cleanup = inspect_pending

    response = do_delete(file_env, file_id)

    assert response.status_code == 204
    assert observed["status"] == "PROCESSING"
    assert observed["claim_token"]
    assert observed["processing_started_at"] is not None
    assert observed["lease_expires_at"] is not None
    assert observed["operation_generation"] == 0
    assert observed["provider_account_id"] == account_id


def test_managed_cleanup_failure_leaves_retryable_pending_row(env):
    fake = _FakeDeleteAdapter("acct-a")
    fake.cleanup_error = StorageUnavailableError(
        "cleanup failed", provider="GOOGLE_DRIVE"
    )
    account_id = seed_account(env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)
    file_id = seed_record(env, "GOOGLE_DRIVE", account_id, "drive-retry")

    assert do_delete(env, file_id).status_code == 204

    rows = pending_rows(env)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "PENDING"
    assert row.retry_count == 1
    assert row.claim_token is None
    assert row.lease_expires_at is None
    assert row.next_retry_at is not None
    assert row.provider_account_id == account_id
    assert "cleanup failed" in (row.last_error or "").lower()
