"""Account-aware download tests for ``GET /api/v1/files/{file_id}/download``.

Covers the H1.5.4 Step 2 integration of ``resolve_file_adapter``:

* legacy (NULL ``provider_account_id``) records keep using the provider
  registry, including existing LOCAL and Google Drive behavior;
* managed records download through their exact account adapter;
* unknown, disconnected, and provider-mismatched accounts fail with the
  approved HTTP status and never consult the legacy registry.

All tests use an isolated in-memory database, fake adapters, and temporary
directories. No credentials, provider APIs, or real application data are used.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base, get_db
from backend.main import app
from backend.models.storage_account import StorageAccount
from backend.models.unified_file import UnifiedFileRecord
from backend.storage import file_account_resolution as resolution
from backend.storage.account_adapter_manager import adapter_manager
from backend.storage.capabilities import CAPABILITY_MAP
from backend.storage.errors import StorageNotFoundError
from backend.storage.google_drive import GoogleDriveAdapter
from backend.storage.local import LocalAdapter
from backend.storage.registry import _adapter_map, _capability_map, register


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _MockDriveAdapter(GoogleDriveAdapter):
    """Google Drive adapter that serves fixed bytes without any API call."""

    def __init__(self, payload: bytes = b"drive-content"):
        super().__init__()
        self.payload = payload
        self.downloads: list[str] = []
        self._drive_service = object()

    def download(self, provider_file_id: str) -> bytes:  # type: ignore[override]
        self.downloads.append(provider_file_id)
        return self.payload


class _ExplodingLocalAdapter(LocalAdapter):
    """Legacy adapter that fails the test if the registry is consulted."""

    def __init__(self, root):
        super().__init__(storage_root=root)
        self.downloads: list[str] = []

    def download(self, provider_file_id: str) -> bytes:  # type: ignore[override]
        self.downloads.append(provider_file_id)
        raise AssertionError("legacy registry must not be used for a managed record")


class _FakeAccountAdapter:
    """Account adapter returned by the manager cache; serves fixed bytes."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.downloads: list[str] = []

    def download(self, provider_file_id: str) -> bytes:
        self.downloads.append(provider_file_id)
        return self.payload


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    """TestClient wired to an isolated DB, with the global manager isolated."""
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

    # The endpoint uses the process-wide manager; point it at this test DB and
    # start from an empty cache. monkeypatch restores the original factory.
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


def register_local(adapter) -> None:
    _adapter_map.pop("LOCAL", None)
    _capability_map.pop("LOCAL", None)
    register("LOCAL", adapter, CAPABILITY_MAP["LOCAL"])


def register_drive(adapter) -> None:
    _adapter_map.pop("GOOGLE_DRIVE", None)
    _capability_map.pop("GOOGLE_DRIVE", None)
    register("GOOGLE_DRIVE", adapter, CAPABILITY_MAP["GOOGLE_DRIVE"])


def make_account(
    env,
    *,
    provider_id: str = "LOCAL",
    status: str = "connected",
    credentials_ref: str | None = None,
    configuration: dict | None = None,
    display_name: str = "Account",
) -> SimpleNamespace:
    session = env.Session()
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
        return SimpleNamespace(
            id=account.id,
            provider_id=account.provider_id,
            status=account.status,
        )
    finally:
        session.close()


def seed_record(
    env,
    *,
    provider: str = "LOCAL",
    account_id: str | None = None,
    provider_file_id: str = "stored-file.bin",
    name: str = "file.txt",
    mime_type: str = "text/plain",
) -> str:
    session = env.Session()
    try:
        record = UnifiedFileRecord(
            provider=provider,
            provider_account_id=account_id,
            provider_file_id=provider_file_id,
            name=name,
            logical_path=f"/{name}",
            mime_type=mime_type,
            size_bytes=10,
            sync_state="LOCAL" if provider == "LOCAL" else "COMPLETED",
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record.id
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Legacy (NULL account) downloads
# ---------------------------------------------------------------------------


def test_legacy_local_download_still_works(env):
    legacy = LocalAdapter(str(env.uploads_dir))
    register_local(legacy)
    (env.uploads_dir / "stored-local.bin").write_bytes(b"legacy-local-bytes")
    file_id = seed_record(env, provider="LOCAL", account_id=None, provider_file_id="stored-local.bin")

    response = env.client.get(f"/api/v1/files/{file_id}/download")

    assert response.status_code == 200
    assert response.content == b"legacy-local-bytes"
    assert response.headers["content-type"].startswith("text/plain")
    assert "filename*=utf-8''file.txt" in response.headers["content-disposition"]


def test_legacy_google_drive_download_still_works(env):
    legacy = _MockDriveAdapter(b"legacy-drive-bytes")
    register_drive(legacy)
    file_id = seed_record(
        env,
        provider="GOOGLE_DRIVE",
        account_id=None,
        provider_file_id="gdrive_legacy_1",
    )

    response = env.client.get(f"/api/v1/files/{file_id}/download")

    assert response.status_code == 200
    assert response.content == b"legacy-drive-bytes"
    assert legacy.downloads == ["gdrive_legacy_1"]


def test_legacy_unregistered_provider_returns_503(env):
    _adapter_map.pop("GOOGLE_DRIVE", None)
    _capability_map.pop("GOOGLE_DRIVE", None)
    file_id = seed_record(
        env, provider="GOOGLE_DRIVE", account_id=None, provider_file_id="gdrive_x"
    )

    response = env.client.get(f"/api/v1/files/{file_id}/download")

    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"].lower()


def test_legacy_provider_not_found_still_returns_404(env):
    class _MissingDrive(_MockDriveAdapter):
        def download(self, provider_file_id: str) -> bytes:  # type: ignore[override]
            raise StorageNotFoundError(provider_file_id, provider="GOOGLE_DRIVE")

    register_drive(_MissingDrive())
    file_id = seed_record(
        env, provider="GOOGLE_DRIVE", account_id=None, provider_file_id="missing"
    )

    response = env.client.get(f"/api/v1/files/{file_id}/download")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Managed-account downloads
# ---------------------------------------------------------------------------


def test_managed_local_download_uses_account_adapter(env):
    account = make_account(env, provider_id="LOCAL", configuration={"storage_root": "/data/a"})
    account_adapter = _FakeAccountAdapter(b"managed-local-bytes")
    adapter_manager.install(account.id, account_adapter)
    file_id = seed_record(
        env, provider="LOCAL", account_id=account.id, provider_file_id="stored-a.bin"
    )

    response = env.client.get(f"/api/v1/files/{file_id}/download")

    assert response.status_code == 200
    assert response.content == b"managed-local-bytes"
    assert account_adapter.downloads == ["stored-a.bin"]


def test_managed_google_drive_download_uses_account_adapter(env):
    account = make_account(
        env,
        provider_id="GOOGLE_DRIVE",
        credentials_ref="ref-a",
    )
    account_adapter = _FakeAccountAdapter(b"managed-drive-bytes")
    adapter_manager.install(account.id, account_adapter)
    file_id = seed_record(
        env,
        provider="GOOGLE_DRIVE",
        account_id=account.id,
        provider_file_id="gdrive_managed_1",
    )

    response = env.client.get(f"/api/v1/files/{file_id}/download")

    assert response.status_code == 200
    assert response.content == b"managed-drive-bytes"
    assert account_adapter.downloads == ["gdrive_managed_1"]


def test_two_managed_accounts_download_independently(env):
    account_a = make_account(env, display_name="A", configuration={"storage_root": "/data/a"})
    account_b = make_account(env, display_name="B", configuration={"storage_root": "/data/b"})
    adapter_a = _FakeAccountAdapter(b"bytes-a")
    adapter_b = _FakeAccountAdapter(b"bytes-b")
    adapter_manager.install(account_a.id, adapter_a)
    adapter_manager.install(account_b.id, adapter_b)
    file_a = seed_record(env, account_id=account_a.id, provider_file_id="file-a.bin")
    file_b = seed_record(env, account_id=account_b.id, provider_file_id="file-b.bin")

    response_a = env.client.get(f"/api/v1/files/{file_a}/download")
    response_b = env.client.get(f"/api/v1/files/{file_b}/download")

    assert response_a.status_code == 200
    assert response_a.content == b"bytes-a"
    assert response_b.status_code == 200
    assert response_b.content == b"bytes-b"
    assert adapter_a.downloads == ["file-a.bin"]
    assert adapter_b.downloads == ["file-b.bin"]


# ---------------------------------------------------------------------------
# Fail-closed account errors
# ---------------------------------------------------------------------------


def test_unknown_account_returns_404_without_registry(env):
    exploding = _ExplodingLocalAdapter(env.uploads_dir)
    register_local(exploding)
    missing = str(uuid.uuid4())
    file_id = seed_record(env, account_id=missing, provider_file_id="stored.bin")

    response = env.client.get(f"/api/v1/files/{file_id}/download")

    assert response.status_code == 404
    assert "account" in response.json()["detail"].lower()
    assert exploding.downloads == []


def test_disconnected_account_returns_503_without_registry(env):
    account = make_account(
        env, status="disconnected", configuration={"storage_root": "/data/a"}
    )
    adapter_manager.install(account.id, _FakeAccountAdapter(b"must-not-serve"))
    exploding = _ExplodingLocalAdapter(env.uploads_dir)
    register_local(exploding)
    file_id = seed_record(env, account_id=account.id, provider_file_id="stored.bin")

    response = env.client.get(f"/api/v1/files/{file_id}/download")

    assert response.status_code == 503
    assert "not available" in response.json()["detail"].lower()
    assert exploding.downloads == []


def test_provider_mismatch_returns_409_without_registry(env):
    account = make_account(env, provider_id="LOCAL", configuration={"storage_root": "/data/a"})
    adapter_manager.install(account.id, _FakeAccountAdapter(b"must-not-serve"))
    exploding = _MockDriveAdapter(b"must-not-serve")
    register_drive(exploding)
    file_id = seed_record(
        env,
        provider="GOOGLE_DRIVE",
        account_id=account.id,
        provider_file_id="gdrive_1",
    )

    response = env.client.get(f"/api/v1/files/{file_id}/download")

    assert response.status_code == 409
    assert "does not match" in response.json()["detail"].lower()
    assert exploding.downloads == []


def test_managed_account_failure_never_consults_registry(env, monkeypatch):
    account = make_account(
        env, provider_id="GOOGLE_DRIVE", credentials_ref="ref-a"
    )
    calls: list[str] = []

    def _forbidden(provider):
        calls.append(provider)
        raise AssertionError("legacy registry must not be consulted")

    monkeypatch.setattr(resolution, "lookup", _forbidden)
    file_id = seed_record(
        env,
        provider="GOOGLE_DRIVE",
        account_id=account.id,
        provider_file_id="gdrive_1",
    )

    # No cached adapter and no usable credentials -> 503, not a registry lookup.
    response = env.client.get(f"/api/v1/files/{file_id}/download")

    assert response.status_code == 503
    assert calls == []


# ---------------------------------------------------------------------------
# Response and activity behavior
# ---------------------------------------------------------------------------


def test_managed_download_records_activity(env):
    account = make_account(env, provider_id="LOCAL", configuration={"storage_root": "/data/a"})
    adapter_manager.install(account.id, _FakeAccountAdapter(b"activity-bytes"))
    file_id = seed_record(
        env,
        account_id=account.id,
        provider_file_id="stored.bin",
        name="activity.txt",
    )

    download = env.client.get(f"/api/v1/files/{file_id}/download")
    activity = env.client.get("/api/v1/activity", params={"action_type": "DOWNLOAD"})

    assert download.status_code == 200
    assert download.content == b"activity-bytes"
    assert activity.status_code == 200
    body = activity.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["action_type"] == "DOWNLOAD"
    assert item["file_id"] == file_id
    assert item["provider"] == "LOCAL"
    assert item["file_name"] == "activity.txt"


def test_managed_download_response_headers_use_record_metadata(env):
    account = make_account(env, provider_id="LOCAL", configuration={"storage_root": "/data/a"})
    adapter_manager.install(account.id, _FakeAccountAdapter(b"pdf-bytes"))
    file_id = seed_record(
        env,
        account_id=account.id,
        provider_file_id="stored.bin",
        name="report.pdf",
        mime_type="application/pdf",
    )

    response = env.client.get(f"/api/v1/files/{file_id}/download")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert "filename*=utf-8''report.pdf" in response.headers["content-disposition"]
