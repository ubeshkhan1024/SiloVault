"""Account-aware upload tests (HVSA H1.5.4 Step 4).

Verifies ``POST /api/v1/uploads``:

* omitting ``account_id`` keeps the legacy provider-registry upload,
* supplying ``account_id`` resolves exactly that managed account and persists
  its ID on the UnifiedFileRecord,
* provider/account mismatches and resolution failures fail closed with no
  upload, no metadata row, no activity, and no registry fallback,
* two accounts of the same provider stay isolated,
* existing upload transaction/compensation behavior is preserved.

Isolated databases, temporary directories, and fake adapters only.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import settings
from backend.database import Base, get_db
from backend.main import app
from backend.models.storage_account import StorageAccount
from backend.models.unified_file import UnifiedFileRecord
from backend.storage.account_adapter_manager import adapter_manager
from backend.storage.capabilities import CAPABILITY_MAP
from backend.storage.errors import (
    StoragePermissionError,
    StorageUnavailableError,
)
from backend.storage.interfaces import StorageOperations
from backend.storage.local import LocalAdapter
from backend.storage.registry import _adapter_map, _capability_map, register
from backend.storage.types import ProviderFileRecord


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeUploadAdapter(StorageOperations):
    """Account-scoped fake adapter recording upload and compensation calls."""

    def __init__(self, tag: str):
        self.tag = tag
        self.upload_calls: list[dict] = []
        self.quarantine_calls: list[str] = []
        self.cleanup_calls: list[str] = []
        self.upload_error: Exception | None = None
        self.quarantine_error: Exception | None = None
        self.cleanup_error: Exception | None = None
        self.provider_file_id: str | None = None
        self.result_name: str | None = None
        self.result_mime: str = "application/octet-stream"
        self.result_size: int | None = None

    def upload(self, file_data, name, logical_path) -> ProviderFileRecord:
        self.upload_calls.append(
            {"data": bytes(file_data), "name": name, "logical_path": logical_path}
        )
        if self.upload_error is not None:
            raise self.upload_error
        provider_file_id = self.provider_file_id or f"pfid-{self.tag}-{len(self.upload_calls)}"
        return ProviderFileRecord(
            provider_file_id=provider_file_id,
            name=self.result_name or name,
            logical_path=logical_path,
            mime_type=self.result_mime,
            size_bytes=(
                self.result_size
                if self.result_size is not None
                else len(file_data)
            ),
        )

    def download(self, provider_file_id: str) -> bytes:
        raise NotImplementedError

    def rename(self, provider_file_id: str, new_name: str) -> ProviderFileRecord:
        raise NotImplementedError

    def move(
        self, source_provider_file_id: str, target_logical_path: str
    ) -> ProviderFileRecord:
        raise NotImplementedError

    def quarantine(self, provider_file_id: str) -> str:
        self.quarantine_calls.append(provider_file_id)
        if self.quarantine_error is not None:
            raise self.quarantine_error
        return f"q::{self.tag}::{provider_file_id}"

    def restore(self, quarantine_id: str) -> ProviderFileRecord:
        raise NotImplementedError

    def cleanup(self, quarantine_id: str) -> None:
        self.cleanup_calls.append(quarantine_id)
        if self.cleanup_error is not None:
            raise self.cleanup_error


class _ExplodingRegistryAdapter(StorageOperations):
    """Provider-level adapter that records and refuses use."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def upload(self, file_data, name, logical_path):
        self.calls.append(("upload", name))
        raise AssertionError("managed upload must not use the legacy registry")

    def download(self, provider_file_id: str) -> bytes:
        raise NotImplementedError

    def rename(self, provider_file_id: str, new_name: str) -> ProviderFileRecord:
        raise NotImplementedError

    def move(
        self, source_provider_file_id: str, target_logical_path: str
    ) -> ProviderFileRecord:
        raise NotImplementedError

    def quarantine(self, provider_file_id: str) -> str:
        self.calls.append(("quarantine", provider_file_id))
        raise AssertionError("managed upload must not use the legacy registry")

    def restore(self, quarantine_id: str):
        raise NotImplementedError

    def cleanup(self, quarantine_id: str) -> None:
        self.calls.append(("cleanup", quarantine_id))
        raise AssertionError("managed upload must not use the legacy registry")


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


@pytest.fixture
def upload_env(tmp_path, monkeypatch):
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
    monkeypatch.setattr(settings, "uploads_dir", str(uploads_dir))

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

    local = LocalAdapter(str(uploads_dir))
    _adapter_map.pop("LOCAL", None)
    _capability_map.pop("LOCAL", None)
    register("LOCAL", local, CAPABILITY_MAP["LOCAL"])

    monkeypatch.setattr(adapter_manager, "_session_factory", TestSession)
    adapter_manager.invalidate_all()

    try:
        yield SimpleNamespace(
            client=TestClient(app, raise_server_exceptions=False),
            engine=engine,
            Session=TestSession,
            uploads=uploads_dir,
            local_adapter=local,
        )
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
) -> str:
    session = env.Session()
    try:
        account = StorageAccount(
            id=str(uuid.uuid4()),
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


def post_upload(
    env,
    filename: str = "file.txt",
    content: bytes = b"content",
    *,
    provider: str | None = None,
    account_id: str | None = None,
    raw_account_id: str | None = None,
):
    params: dict[str, str] = {}
    if provider is not None:
        params["provider"] = provider
    if raw_account_id is not None:
        params["account_id"] = raw_account_id
    elif account_id is not None:
        params["account_id"] = account_id
    files = {"file": (filename, content, "application/octet-stream")}
    return env.client.post("/api/v1/uploads", files=files, params=params)


def all_records(env) -> list[UnifiedFileRecord]:
    session = env.Session()
    try:
        return list(session.execute(select(UnifiedFileRecord)).scalars().all())
    finally:
        session.close()


def activity_items(env) -> list[dict]:
    return env.client.get("/api/v1/activity").json()["items"]


# ---------------------------------------------------------------------------
# A. Legacy compatibility
# ---------------------------------------------------------------------------


def test_legacy_local_upload_omitting_account_id(upload_env):
    response = post_upload(upload_env, "legacy.txt", b"legacy bytes")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "LOCAL"
    assert payload["provider_account_id"] is None
    assert payload["name"] == "legacy.txt"
    assert payload["size_bytes"] == len(b"legacy bytes")
    assert payload["sha256"] == hashlib.sha256(b"legacy bytes").hexdigest()
    assert payload["sync_state"] == "LOCAL"
    assert (upload_env.uploads / payload["provider_file_id"]).read_bytes() == b"legacy bytes"

    records = all_records(upload_env)
    assert len(records) == 1
    assert records[0].provider_account_id is None

    activities = activity_items(upload_env)
    assert len(activities) == 1
    assert activities[0]["action_type"] == "UPLOAD"


def test_legacy_drive_upload_uses_provider_registry(upload_env):
    fake = _FakeUploadAdapter("legacy-drive")
    set_registry_adapter("GOOGLE_DRIVE", fake)

    response = post_upload(
        upload_env, "cloud.txt", b"drive bytes", provider="GOOGLE_DRIVE"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider_account_id"] is None
    assert fake.upload_calls[0]["data"] == b"drive bytes"
    assert fake.upload_calls[0]["name"] == "cloud.txt"
    assert len(fake.upload_calls) == 1

    record = all_records(upload_env)[0]
    assert record.provider_account_id is None
    assert record.provider_file_id == payload["provider_file_id"]


def test_legacy_upload_validation_is_unchanged(upload_env):
    response = post_upload(upload_env, "bad.txt", b"x", provider="local")

    assert response.status_code == 422
    assert all_records(upload_env) == []
    assert activity_items(upload_env) == []


def test_legacy_upload_oversize_returns_413(upload_env, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_size_bytes", 3)

    response = post_upload(upload_env, "big.bin", b"0123456789")

    assert response.status_code == 413
    assert all_records(upload_env) == []


# ---------------------------------------------------------------------------
# B. Managed-account upload
# ---------------------------------------------------------------------------


def test_managed_local_upload_persists_account_and_adapter_metadata(upload_env):
    fake = _FakeUploadAdapter("acct-local")
    fake.result_name = "renamed-by-adapter.txt"
    fake.result_mime = "text/custom"
    fake.result_size = 999
    account_id = seed_account(upload_env, "LOCAL")
    adapter_manager.install(account_id, fake)
    legacy = _ExplodingRegistryAdapter()
    set_registry_adapter("LOCAL", legacy)

    content = b"managed local bytes"
    response = post_upload(
        upload_env, "original.txt", content, account_id=account_id
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "LOCAL"
    assert payload["provider_account_id"] == account_id
    assert payload["provider_file_id"] == "pfid-acct-local-1"
    assert payload["name"] == "renamed-by-adapter.txt"
    assert payload["mime_type"] == "text/custom"
    assert payload["size_bytes"] == 999
    assert payload["sha256"] == hashlib.sha256(content).hexdigest()
    assert payload["sync_state"] == "LOCAL"

    assert fake.upload_calls == [
        {"data": content, "name": "original.txt", "logical_path": "/original.txt"}
    ]
    assert legacy.calls == []

    record = all_records(upload_env)[0]
    assert record.provider_account_id == account_id
    assert record.provider_file_id == "pfid-acct-local-1"
    assert record.name == "renamed-by-adapter.txt"
    assert record.size_bytes == 999

    activities = activity_items(upload_env)
    assert len(activities) == 1
    assert activities[0]["action_type"] == "UPLOAD"
    assert activities[0]["file_id"] == record.id
    assert activities[0]["provider"] == "LOCAL"


def test_managed_drive_upload_uses_exact_account(upload_env):
    fake = _FakeUploadAdapter("acct-drive")
    fake.result_mime = "application/pdf"
    account_id = seed_account(upload_env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)
    legacy = _ExplodingRegistryAdapter()
    set_registry_adapter("GOOGLE_DRIVE", legacy)

    response = post_upload(
        upload_env,
        "cloud.pdf",
        b"%PDF-managed",
        provider="GOOGLE_DRIVE",
        account_id=account_id,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "GOOGLE_DRIVE"
    assert payload["provider_account_id"] == account_id
    assert payload["provider_file_id"] == "pfid-acct-drive-1"
    assert payload["sync_state"] == "PENDING"

    assert fake.upload_calls[0]["data"] == b"%PDF-managed"
    assert legacy.calls == []
    assert all_records(upload_env)[0].provider_account_id == account_id


# ---------------------------------------------------------------------------
# C. Multi-account isolation
# ---------------------------------------------------------------------------


def test_two_accounts_same_provider_are_isolated(upload_env):
    fake_a = _FakeUploadAdapter("acct-a")
    fake_b = _FakeUploadAdapter("acct-b")
    account_a = seed_account(upload_env, "GOOGLE_DRIVE")
    account_b = seed_account(upload_env, "GOOGLE_DRIVE")
    adapter_manager.install(account_a, fake_a)
    adapter_manager.install(account_b, fake_b)
    legacy = _ExplodingRegistryAdapter()
    set_registry_adapter("GOOGLE_DRIVE", legacy)

    response_a = post_upload(
        upload_env, "same.txt", b"A", provider="GOOGLE_DRIVE", account_id=account_a
    )
    response_b = post_upload(
        upload_env, "same.txt", b"B", provider="GOOGLE_DRIVE", account_id=account_b
    )

    assert response_a.status_code == 200
    assert response_b.status_code == 200
    assert fake_a.upload_calls == [
        {"data": b"A", "name": "same.txt", "logical_path": "/same.txt"}
    ]
    assert fake_b.upload_calls == [
        {"data": b"B", "name": "same.txt", "logical_path": "/same.txt"}
    ]
    assert legacy.calls == []

    records = all_records(upload_env)
    assert {r.provider_account_id for r in records} == {account_a, account_b}
    assert len({r.provider_file_id for r in records}) == 2


def test_invalidating_one_account_does_not_affect_another(upload_env):
    fake_a = _FakeUploadAdapter("acct-a")
    fake_b = _FakeUploadAdapter("acct-b")
    account_a = seed_account(upload_env, "GOOGLE_DRIVE")
    account_b = seed_account(upload_env, "GOOGLE_DRIVE")
    adapter_manager.install(account_a, fake_a)
    adapter_manager.install(account_b, fake_b)

    adapter_manager.invalidate_account(account_a)

    assert adapter_manager.cache.get(account_a) is None
    assert adapter_manager.cache.get(account_b) is fake_b

    response = post_upload(
        upload_env, "b.txt", b"B", provider="GOOGLE_DRIVE", account_id=account_b
    )

    assert response.status_code == 200
    assert fake_b.upload_calls
    assert fake_a.upload_calls == []


# ---------------------------------------------------------------------------
# D. Fail-closed behavior
# ---------------------------------------------------------------------------


def test_unknown_account_returns_404_without_upload(upload_env):
    legacy = _ExplodingRegistryAdapter()
    set_registry_adapter("GOOGLE_DRIVE", legacy)
    missing = str(uuid.uuid4())

    response = post_upload(
        upload_env, "x.txt", b"x", provider="GOOGLE_DRIVE", account_id=missing
    )

    assert response.status_code == 404
    assert missing not in response.text
    assert all_records(upload_env) == []
    assert activity_items(upload_env) == []
    assert legacy.calls == []


def test_disconnected_account_returns_503_without_upload(upload_env):
    fake = _FakeUploadAdapter("disconnected")
    account_id = seed_account(upload_env, "GOOGLE_DRIVE", status="disconnected")
    adapter_manager.install(account_id, fake)

    response = post_upload(
        upload_env, "x.txt", b"x", provider="GOOGLE_DRIVE", account_id=account_id
    )

    assert response.status_code == 503
    assert fake.upload_calls == []
    assert all_records(upload_env) == []
    assert activity_items(upload_env) == []


def test_provider_mismatch_returns_409_without_upload(upload_env):
    fake = _FakeUploadAdapter("mismatch")
    account_id = seed_account(upload_env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)

    response = post_upload(
        upload_env, "x.txt", b"x", provider="LOCAL", account_id=account_id
    )

    assert response.status_code == 409
    assert fake.upload_calls == []
    assert all_records(upload_env) == []
    assert activity_items(upload_env) == []


def test_storage_unavailable_during_resolution_returns_503(upload_env, monkeypatch):
    account_id = seed_account(upload_env, "GOOGLE_DRIVE")

    def unavailable(_account_id):
        raise StorageUnavailableError(
            detail="credential store unavailable", provider="GOOGLE_DRIVE"
        )

    monkeypatch.setattr(adapter_manager, "resolve", unavailable)

    response = post_upload(
        upload_env, "x.txt", b"x", provider="GOOGLE_DRIVE", account_id=account_id
    )

    assert response.status_code == 503
    assert all_records(upload_env) == []
    assert activity_items(upload_env) == []


def test_generic_resolution_failure_fails_closed(upload_env, monkeypatch):
    account_id = seed_account(upload_env, "GOOGLE_DRIVE")
    legacy = _ExplodingRegistryAdapter()
    set_registry_adapter("GOOGLE_DRIVE", legacy)

    def boom(_account_id):
        raise RuntimeError("unexpected resolver failure")

    monkeypatch.setattr(adapter_manager, "resolve", boom)

    response = post_upload(
        upload_env, "x.txt", b"x", provider="GOOGLE_DRIVE", account_id=account_id
    )

    assert response.status_code == 500
    assert all_records(upload_env) == []
    assert activity_items(upload_env) == []
    assert legacy.calls == []


def test_empty_account_id_is_rejected_not_treated_as_legacy(upload_env):
    legacy = _ExplodingRegistryAdapter()
    set_registry_adapter("LOCAL", legacy)

    response = post_upload(upload_env, "x.txt", b"x", raw_account_id="   ")

    assert response.status_code == 422
    assert "account_id" in response.json()["detail"]
    assert all_records(upload_env) == []
    assert legacy.calls == []


def test_account_deleted_before_upload_returns_404(upload_env):
    fake = _FakeUploadAdapter("gone")
    account_id = seed_account(upload_env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)
    delete_account(upload_env, account_id)

    response = post_upload(
        upload_env, "x.txt", b"x", provider="GOOGLE_DRIVE", account_id=account_id
    )

    assert response.status_code == 404
    assert fake.upload_calls == []
    assert all_records(upload_env) == []


# ---------------------------------------------------------------------------
# E. Persistence and rollback
# ---------------------------------------------------------------------------


def test_managed_upload_db_commit_failure_compensates_via_account_adapter(
    upload_env, monkeypatch
):
    fake = _FakeUploadAdapter("acct-local")
    account_id = seed_account(upload_env, "LOCAL")
    adapter_manager.install(account_id, fake)

    def failing_commit(_session):
        raise RuntimeError("db commit failed")

    monkeypatch.setattr(OrmSession, "commit", failing_commit)

    response = post_upload(upload_env, "rollback.txt", b"data", account_id=account_id)

    assert response.status_code == 500
    assert "cleanup" in response.json()["detail"].lower()
    assert fake.quarantine_calls == ["pfid-acct-local-1"]
    assert fake.cleanup_calls == ["q::acct-local::pfid-acct-local-1"]
    assert all_records(upload_env) == []
    assert activity_items(upload_env) == []


def test_managed_upload_compensation_failure_is_logged(
    upload_env, monkeypatch, caplog
):
    fake = _FakeUploadAdapter("acct-local")
    fake.quarantine_error = StorageUnavailableError(
        "quarantine failed", provider="LOCAL"
    )
    account_id = seed_account(upload_env, "LOCAL")
    adapter_manager.install(account_id, fake)

    def failing_commit(_session):
        raise RuntimeError("db commit failed")

    monkeypatch.setattr(OrmSession, "commit", failing_commit)
    caplog.set_level(logging.ERROR, logger="backend.api.v1.endpoints.uploads")

    response = post_upload(upload_env, "rollback.txt", b"data", account_id=account_id)

    assert response.status_code == 500
    assert "cleanup" in response.json()["detail"].lower()
    assert "db commit failed" not in response.text
    assert fake.upload_calls
    assert all_records(upload_env) == []
    assert activity_items(upload_env) == []
    assert any(
        "failed to quarantine/cleanup" in record.getMessage()
        for record in caplog.records
    )


def test_duplicate_provider_file_id_is_rejected(upload_env):
    fake = _FakeUploadAdapter("acct-drive")
    fake.provider_file_id = "duplicate-id"
    account_id = seed_account(upload_env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)

    first = post_upload(
        upload_env, "one.txt", b"1", provider="GOOGLE_DRIVE", account_id=account_id
    )
    second = post_upload(
        upload_env, "two.txt", b"2", provider="GOOGLE_DRIVE", account_id=account_id
    )

    assert first.status_code == 200
    assert second.status_code == 500
    records = all_records(upload_env)
    assert len(records) == 1
    assert records[0].provider_file_id == "duplicate-id"
    assert len(fake.upload_calls) == 2
    assert len(activity_items(upload_env)) == 1


def test_managed_upload_provider_error_mapping_is_preserved(upload_env):
    fake = _FakeUploadAdapter("acct-drive")
    fake.upload_error = StoragePermissionError(provider="GOOGLE_DRIVE")
    account_id = seed_account(upload_env, "GOOGLE_DRIVE")
    adapter_manager.install(account_id, fake)

    response = post_upload(
        upload_env, "x.txt", b"x", provider="GOOGLE_DRIVE", account_id=account_id
    )

    assert response.status_code == 403
    assert all_records(upload_env) == []
    assert activity_items(upload_env) == []
