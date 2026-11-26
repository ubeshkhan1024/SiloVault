"""StorageUnavailableError response sanitization (HVSA H1.5.4 Step 6).

``StorageUnavailableError`` details are produced by the storage and account
layers and can carry internal account IDs, credential references, keyring or
provider diagnostics.  These tests prove that every file/upload endpoint that
maps that error returns HTTP 503 with the single fixed public message, and that
no part of the exception string reaches the response body.

They also pin the surrounding behavior: the 404 / 409 / "not connected" 503
mappings are unchanged, the original exception stays attached as ``__cause__``
for internal diagnostics, legacy (NULL account) records still use the provider
registry, and managed records never fall back to it.

Isolated in-memory database, fake adapters and a temporary directory only.  No
credentials, provider APIs, or real application data are involved.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.v1.endpoints.account_adapter_http import (
    STORAGE_UNAVAILABLE_DETAIL,
    resolve_adapter_or_http,
)
from backend.database import Base, get_db
from backend.main import app
from backend.models.pending_deletion import PendingDeletion
from backend.models.storage_account import StorageAccount
from backend.models.unified_file import UnifiedFileRecord
from backend.storage import file_account_resolution as resolution
from backend.storage.account_adapter_manager import adapter_manager
from backend.storage.capabilities import CAPABILITY_MAP
from backend.storage.errors import StorageUnavailableError
from backend.storage.interfaces import StorageOperations
from backend.storage.local import LocalAdapter
from backend.storage.registry import _adapter_map, _capability_map, register
from backend.storage.types import ProviderFileRecord

#: Text that must never appear in a response body.  It mimics the real detail
#: strings raised by ``AccountAdapterManager`` and by cloud adapters.
SECRET_CREDENTIAL_REF = "storage-account:should-never-be-public"
SECRET_TOKEN = "ya29.a0AfH6SMB-secret-token"
SECRET_PATH = "/srv/private/storage-root"

OPERATIONS = ["download", "delete", "rename", "move", "upload"]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeAccountAdapter(StorageOperations):
    """Account-scoped adapter that raises a configured error on any operation."""

    def __init__(self) -> None:
        self.error: Exception | None = None
        self.calls: list[str] = []

    def _maybe_raise(self, operation: str) -> None:
        self.calls.append(operation)
        if self.error is not None:
            raise self.error

    def upload(self, file_data, name, logical_path) -> ProviderFileRecord:
        self._maybe_raise("upload")
        return ProviderFileRecord(
            provider_file_id="pfid-1",
            name=name,
            logical_path=logical_path,
            mime_type="text/plain",
            size_bytes=len(file_data),
        )

    def download(self, provider_file_id: str) -> bytes:
        self._maybe_raise("download")
        return b"payload"

    def rename(self, provider_file_id: str, new_name: str) -> ProviderFileRecord:
        self._maybe_raise("rename")
        return ProviderFileRecord(
            provider_file_id=provider_file_id,
            name=new_name,
            logical_path="/renamed.txt",
            mime_type="text/plain",
            size_bytes=1,
        )

    def move(
        self, source_provider_file_id: str, target_logical_path: str
    ) -> ProviderFileRecord:
        self._maybe_raise("move")
        return ProviderFileRecord(
            provider_file_id=source_provider_file_id,
            name="file.txt",
            logical_path=target_logical_path,
            mime_type="text/plain",
            size_bytes=1,
        )

    def quarantine(self, provider_file_id: str) -> str:
        self._maybe_raise("quarantine")
        return "quarantine-1"

    def restore(self, quarantine_id: str) -> ProviderFileRecord:
        self._maybe_raise("restore")
        return ProviderFileRecord(
            provider_file_id="restored",
            name="restored.txt",
            logical_path="/restored.txt",
            mime_type="text/plain",
            size_bytes=1,
        )

    def cleanup(self, quarantine_id: str) -> None:
        self._maybe_raise("cleanup")


def _leaky_error(account_id: str) -> StorageUnavailableError:
    """A realistic StorageUnavailableError carrying every kind of internal text."""

    return StorageUnavailableError(
        detail=(
            f"Credential store unavailable for account {account_id!r}: "
            f"keyring locked for ref {SECRET_CREDENTIAL_REF}; "
            f"refresh_token={SECRET_TOKEN}; root={SECRET_PATH}/{account_id}"
        ),
        provider="LOCAL",
    )


def _forbid_registry(monkeypatch) -> list[str]:
    """Make any provider-registry lookup fail the test and record the attempt."""

    calls: list[str] = []

    def _forbidden(provider):
        calls.append(provider)
        raise AssertionError("legacy registry must not be consulted")

    monkeypatch.setattr(resolution, "lookup", _forbidden)
    return calls


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()

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

    _adapter_map.pop("LOCAL", None)
    _capability_map.pop("LOCAL", None)
    register("LOCAL", LocalAdapter(str(uploads_dir)), CAPABILITY_MAP["LOCAL"])

    monkeypatch.setattr(adapter_manager, "_session_factory", TestSession)
    adapter_manager.invalidate_all()

    try:
        yield SimpleNamespace(
            client=TestClient(app, raise_server_exceptions=False),
            engine=engine,
            Session=TestSession,
            uploads_dir=uploads_dir,
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


def seed_account(
    env,
    provider_id: str = "LOCAL",
    *,
    status: str = "connected",
) -> str:
    session = env.Session()
    try:
        account = StorageAccount(
            id=str(uuid.uuid4()),
            provider_id=provider_id,
            display_name=f"{provider_id} account",
            configuration={},
            status=status,
        )
        session.add(account)
        session.commit()
        return account.id
    finally:
        session.close()


def seed_record(
    env,
    *,
    provider: str = "LOCAL",
    account_id: str | None = None,
    provider_file_id: str = "stored-file.bin",
) -> str:
    session = env.Session()
    try:
        record = UnifiedFileRecord(
            provider=provider,
            provider_account_id=account_id,
            provider_file_id=provider_file_id,
            name="file.txt",
            logical_path="/file.txt",
            mime_type="text/plain",
            size_bytes=1,
            sync_state="LOCAL" if provider == "LOCAL" else "COMPLETED",
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record.id
    finally:
        session.close()


def perform(
    env,
    operation: str,
    file_id: str | None,
    *,
    account_id: str | None = None,
    provider: str = "LOCAL",
):
    if operation == "download":
        return env.client.get(f"/api/v1/files/{file_id}/download")
    if operation == "delete":
        return env.client.delete(f"/api/v1/files/{file_id}")
    if operation == "rename":
        return env.client.patch(
            f"/api/v1/files/{file_id}/rename", json={"new_name": "renamed.txt"}
        )
    if operation == "move":
        return env.client.patch(
            f"/api/v1/files/{file_id}/move",
            json={"target_logical_path": "/moved/file.txt"},
        )
    params: dict[str, str] = {"provider": provider}
    if account_id is not None:
        params["account_id"] = account_id
    return env.client.post(
        "/api/v1/uploads",
        files={"file": ("upload.txt", b"payload", "text/plain")},
        params=params,
    )


def record_exists(env, file_id: str) -> bool:
    session = env.Session()
    try:
        return session.get(UnifiedFileRecord, file_id) is not None
    finally:
        session.close()


def record_count(env) -> int:
    session = env.Session()
    try:
        return len(session.execute(select(UnifiedFileRecord)).scalars().all())
    finally:
        session.close()


def pending_count(env) -> int:
    session = env.Session()
    try:
        return len(session.execute(select(PendingDeletion)).scalars().all())
    finally:
        session.close()


def assert_sanitized(response, account_id: str) -> None:
    """The only thing a client may see is the fixed public message."""

    assert response.status_code == 503
    assert response.json()["detail"] == STORAGE_UNAVAILABLE_DETAIL
    assert STORAGE_UNAVAILABLE_DETAIL == "Storage provider is temporarily unavailable."
    for marker in (
        account_id,
        SECRET_CREDENTIAL_REF,
        SECRET_TOKEN,
        SECRET_PATH,
        "keyring",
        "Credential store",
    ):
        assert marker not in response.text


# ---------------------------------------------------------------------------
# 1-5. Managed resolution failure (account-aware layer)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation", OPERATIONS)
def test_managed_resolution_unavailable_is_sanitized(env, operation, monkeypatch):
    account_id = seed_account(env)
    file_id = (
        None
        if operation == "upload"
        else seed_record(env, account_id=account_id, provider_file_id="stored.bin")
    )
    leaky = _leaky_error(account_id)
    registry_calls = _forbid_registry(monkeypatch)

    def _unavailable(_account_id: str):
        raise leaky

    monkeypatch.setattr(adapter_manager, "resolve", _unavailable)

    response = perform(env, operation, file_id, account_id=account_id)

    assert_sanitized(response, account_id)
    assert registry_calls == []


# ---------------------------------------------------------------------------
# 1-5. Adapter operation failure (provider layer)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation", OPERATIONS)
def test_adapter_operation_unavailable_is_sanitized(env, operation):
    account_id = seed_account(env)
    fake = _FakeAccountAdapter()
    fake.error = _leaky_error(account_id)
    adapter_manager.install(account_id, fake)
    file_id = (
        None
        if operation == "upload"
        else seed_record(env, account_id=account_id, provider_file_id="stored.bin")
    )

    response = perform(env, operation, file_id, account_id=account_id)

    assert_sanitized(response, account_id)

    # The failure must not leave partial state behind.
    if operation == "upload":
        assert record_count(env) == 0
    elif operation == "delete":
        assert record_exists(env, file_id) is True
        assert pending_count(env) == 0


# ---------------------------------------------------------------------------
# 1-5. Legacy (NULL account) registry failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation", OPERATIONS)
def test_legacy_unregistered_provider_is_sanitized(env, operation):
    _adapter_map.pop("GOOGLE_DRIVE", None)
    _capability_map.pop("GOOGLE_DRIVE", None)
    file_id = (
        None
        if operation == "upload"
        else seed_record(
            env, provider="GOOGLE_DRIVE", account_id=None, provider_file_id="gdrive-1"
        )
    )

    response = perform(env, operation, file_id, provider="GOOGLE_DRIVE")

    assert response.status_code == 503
    assert response.json()["detail"] == STORAGE_UNAVAILABLE_DETAIL
    assert "not registered" not in response.text
    assert "legacy registry" not in response.text


# ---------------------------------------------------------------------------
# 6. Existing mappings are unchanged
# ---------------------------------------------------------------------------


def test_missing_account_still_returns_404(env):
    missing = str(uuid.uuid4())
    file_id = seed_record(env, account_id=missing)

    response = env.client.get(f"/api/v1/files/{file_id}/download")

    assert response.status_code == 404
    assert response.json()["detail"] == "The storage account for this file was not found."
    assert missing not in response.text


def test_provider_mismatch_still_returns_409(env):
    account_id = seed_account(env, provider_id="LOCAL")
    file_id = seed_record(env, provider="GOOGLE_DRIVE", account_id=account_id)

    response = env.client.get(f"/api/v1/files/{file_id}/download")

    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == "The storage account does not match this file's storage provider."
    )
    assert account_id not in response.text


def test_not_connected_still_returns_503(env):
    account_id = seed_account(env, status="disconnected")
    adapter_manager.install(account_id, _FakeAccountAdapter())
    file_id = seed_record(env, account_id=account_id)

    response = env.client.get(f"/api/v1/files/{file_id}/download")

    assert response.status_code == 503
    assert response.json()["detail"] == "The storage account for this file is not available."
    assert response.json()["detail"] != STORAGE_UNAVAILABLE_DETAIL
    assert account_id not in response.text


# ---------------------------------------------------------------------------
# Diagnostics are preserved internally
# ---------------------------------------------------------------------------


def test_original_exception_is_preserved_for_diagnostics(env, monkeypatch):
    account_id = seed_account(env)
    record = SimpleNamespace(provider="LOCAL", provider_account_id=account_id)
    leaky = _leaky_error(account_id)

    def _unavailable(_account_id: str):
        raise leaky

    monkeypatch.setattr(adapter_manager, "resolve", _unavailable)

    session = env.Session()
    try:
        with pytest.raises(HTTPException) as excinfo:
            resolve_adapter_or_http(session, record)
    finally:
        session.close()

    assert excinfo.value.status_code == 503
    assert excinfo.value.detail == STORAGE_UNAVAILABLE_DETAIL
    # The public message is fixed, but the real error is still chained for logs.
    assert excinfo.value.__cause__ is leaky
    assert account_id in str(excinfo.value.__cause__)


# ---------------------------------------------------------------------------
# 7. Legacy NULL-account behavior is unchanged
# ---------------------------------------------------------------------------


def test_legacy_local_upload_and_download_still_work(env):
    upload = env.client.post(
        "/api/v1/uploads",
        files={"file": ("legacy.txt", b"legacy bytes", "text/plain")},
        params={"provider": "LOCAL"},
    )
    assert upload.status_code == 200
    payload = upload.json()
    assert payload["provider_account_id"] is None

    download = env.client.get(f"/api/v1/files/{payload['id']}/download")
    assert download.status_code == 200
    assert download.content == b"legacy bytes"


def test_legacy_local_rename_and_move_still_work(env):
    file_id = seed_record(env)
    (env.uploads_dir / "stored-file.bin").write_bytes(b"legacy")

    rename = env.client.patch(
        f"/api/v1/files/{file_id}/rename", json={"new_name": "renamed.txt"}
    )
    assert rename.status_code == 200
    assert rename.json()["name"] == "renamed.txt"

    move = env.client.patch(
        f"/api/v1/files/{file_id}/move",
        json={"target_logical_path": "/archive/file.txt"},
    )
    assert move.status_code == 200
    assert move.json()["logical_path"] == "/archive/file.txt"


# ---------------------------------------------------------------------------
# 8. Managed resolution still fails closed
# ---------------------------------------------------------------------------


def test_managed_unavailable_never_consults_registry(env, monkeypatch):
    account_id = seed_account(env)
    file_id = seed_record(env, account_id=account_id)
    registry_calls = _forbid_registry(monkeypatch)

    def _unavailable(_account_id: str):
        raise _leaky_error(account_id)

    monkeypatch.setattr(adapter_manager, "resolve", _unavailable)

    response = env.client.get(f"/api/v1/files/{file_id}/download")

    assert response.status_code == 503
    assert registry_calls == []
    assert record_exists(env, file_id) is True
