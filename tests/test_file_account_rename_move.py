"""Account-aware rename and move tests (HVSA H1.5.4 Step 3A).

Verifies that ``PATCH /api/v1/files/{file_id}/rename`` and
``PATCH /api/v1/files/{file_id}/move``:

* keep legacy NULL-account records on the provider registry,
* resolve managed records through their exact account adapter,
* fail closed on unknown, disconnected, or provider-mismatched accounts,
* never fall back to the legacy registry for a managed record,
* preserve metadata updates, activity logging, and provider error mappings.

All tests use an isolated in-memory database, fake adapters, and temporary
directories. No credentials or provider APIs are used.
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
from backend.storage.errors import (
    StorageAuthenticationError,
    StorageNotFoundError,
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


class _FakeAccountAdapter:
    """Account-scoped fake adapter; records the exact rename/move calls."""

    def __init__(self):
        self.rename_calls: list[tuple[str, str]] = []
        self.move_calls: list[tuple[str, str]] = []
        self.rename_error: Exception | None = None
        self.move_error: Exception | None = None

    def rename(self, provider_file_id: str, new_name: str) -> ProviderFileRecord:
        self.rename_calls.append((provider_file_id, new_name))
        if self.rename_error is not None:
            raise self.rename_error
        return ProviderFileRecord(
            provider_file_id=provider_file_id,
            name=new_name,
            logical_path="/stored/path.txt",
            mime_type="text/plain",
            size_bytes=10,
        )

    def move(
        self, source_provider_file_id: str, target_logical_path: str
    ) -> ProviderFileRecord:
        self.move_calls.append((source_provider_file_id, target_logical_path))
        if self.move_error is not None:
            raise self.move_error
        return ProviderFileRecord(
            provider_file_id=source_provider_file_id,
            name="file.txt",
            logical_path=target_logical_path,
            mime_type="text/plain",
            size_bytes=10,
        )


class _RecordingDriveAdapter(StorageOperations):
    """Legacy Google Drive fake registered in the provider registry."""

    def __init__(self):
        self.rename_calls: list[tuple[str, str]] = []
        self.move_calls: list[tuple[str, str]] = []

    def upload(self, file_data, name, logical_path):
        raise NotImplementedError

    def download(self, provider_file_id: str) -> bytes:
        raise NotImplementedError

    def rename(self, provider_file_id: str, new_name: str) -> ProviderFileRecord:
        self.rename_calls.append((provider_file_id, new_name))
        return ProviderFileRecord(
            provider_file_id=provider_file_id,
            name=new_name,
            logical_path="/drive/path.txt",
            mime_type="application/pdf",
            size_bytes=2048,
        )

    def move(
        self, source_provider_file_id: str, target_logical_path: str
    ) -> ProviderFileRecord:
        self.move_calls.append((source_provider_file_id, target_logical_path))
        return ProviderFileRecord(
            provider_file_id=source_provider_file_id,
            name="drive.pdf",
            logical_path=target_logical_path,
            mime_type="application/pdf",
            size_bytes=2048,
        )

    def quarantine(self, provider_file_id: str) -> str:
        raise NotImplementedError

    def restore(self, quarantine_id: str):
        raise NotImplementedError

    def cleanup(self, quarantine_id: str) -> None:
        raise NotImplementedError


class _ExplodingLocalAdapter(LocalAdapter):
    """Legacy LOCAL adapter that fails the test if the registry is used."""

    def __init__(self, root):
        super().__init__(storage_root=root)
        self.rename_calls: list[tuple[str, str]] = []
        self.move_calls: list[tuple[str, str]] = []

    def rename(self, provider_file_id: str, new_name: str) -> ProviderFileRecord:
        self.rename_calls.append((provider_file_id, new_name))
        raise AssertionError("legacy registry must not be used for a managed record")

    def move(self, source_provider_file_id: str, target_logical_path: str) -> ProviderFileRecord:
        self.move_calls.append((source_provider_file_id, target_logical_path))
        raise AssertionError("legacy registry must not be used for a managed record")


class _ExplodingDriveAdapter(StorageOperations):
    """Legacy Drive adapter that fails the test if the registry is used."""

    def upload(self, file_data, name, logical_path):
        raise NotImplementedError

    def download(self, provider_file_id: str) -> bytes:
        raise NotImplementedError

    def rename(self, provider_file_id: str, new_name: str) -> ProviderFileRecord:
        raise AssertionError("legacy registry must not be used for a managed record")

    def move(self, source_provider_file_id: str, target_logical_path: str) -> ProviderFileRecord:
        raise AssertionError("legacy registry must not be used for a managed record")

    def quarantine(self, provider_file_id: str) -> str:
        raise NotImplementedError

    def restore(self, quarantine_id: str):
        raise NotImplementedError

    def cleanup(self, quarantine_id: str) -> None:
        raise NotImplementedError


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
        return SimpleNamespace(id=account.id, provider_id=account.provider_id)
    finally:
        session.close()


def seed_record(
    env,
    *,
    provider: str = "LOCAL",
    account_id: str | None = None,
    provider_file_id: str = "stored.bin",
    name: str = "file.txt",
    logical_path: str = "/file.txt",
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


def fetch(env, file_id: str) -> dict:
    session = env.Session()
    try:
        record = session.get(UnifiedFileRecord, file_id)
        assert record is not None
        return {
            "name": record.name,
            "logical_path": record.logical_path,
            "provider": record.provider,
            "provider_account_id": record.provider_account_id,
            "provider_file_id": record.provider_file_id,
            "mime_type": record.mime_type,
            "size_bytes": record.size_bytes,
            "sha256": record.sha256,
            "category": record.category,
            "tags": list(record.tags),
        }
    finally:
        session.close()


def do_rename(env, file_id: str, new_name: str = "renamed.txt"):
    return env.client.patch(
        f"/api/v1/files/{file_id}/rename", json={"new_name": new_name}
    )


def do_move(env, file_id: str, path: str = "/New/Folder/moved.txt"):
    return env.client.patch(
        f"/api/v1/files/{file_id}/move", json={"target_logical_path": path}
    )


def perform(env, op: str, file_id: str):
    return do_rename(env, file_id) if op == "rename" else do_move(env, file_id)


OPERATIONS = [
    pytest.param("rename", id="rename"),
    pytest.param("move", id="move"),
]


# ---------------------------------------------------------------------------
# Legacy (NULL account) rename and move
# ---------------------------------------------------------------------------


def test_legacy_local_rename_still_works(env):
    register_local(LocalAdapter(str(env.uploads_dir)))
    (env.uploads_dir / "stored-local.bin").write_bytes(b"legacy")
    file_id = seed_record(env, provider_file_id="stored-local.bin")

    response = do_rename(env, file_id, "legacy-renamed.txt")

    assert response.status_code == 200
    assert response.json()["name"] == "legacy-renamed.txt"
    assert fetch(env, file_id)["name"] == "legacy-renamed.txt"
    # LOCAL rename never changes the physical filename.
    assert (env.uploads_dir / "stored-local.bin").read_bytes() == b"legacy"


def test_legacy_local_move_still_works(env):
    register_local(LocalAdapter(str(env.uploads_dir)))
    (env.uploads_dir / "stored-local.bin").write_bytes(b"legacy")
    file_id = seed_record(env, provider_file_id="stored-local.bin")

    response = do_move(env, file_id, "/Archive/legacy.txt")

    assert response.status_code == 200
    assert response.json()["logical_path"] == "/Archive/legacy.txt"
    assert fetch(env, file_id)["logical_path"] == "/Archive/legacy.txt"
    assert (env.uploads_dir / "stored-local.bin").exists()


def test_legacy_google_drive_rename_still_works(env):
    legacy = _RecordingDriveAdapter()
    register_drive(legacy)
    file_id = seed_record(
        env, provider="GOOGLE_DRIVE", provider_file_id="gdrive_1", name="drive.pdf"
    )

    response = do_rename(env, file_id, "drive-renamed.pdf")

    assert response.status_code == 200
    assert response.json()["name"] == "drive-renamed.pdf"
    assert legacy.rename_calls == [("gdrive_1", "drive-renamed.pdf")]


def test_legacy_google_drive_move_still_works(env):
    legacy = _RecordingDriveAdapter()
    register_drive(legacy)
    file_id = seed_record(
        env, provider="GOOGLE_DRIVE", provider_file_id="gdrive_1", name="drive.pdf"
    )

    response = do_move(env, file_id, "/Drive/Archive/drive.pdf")

    assert response.status_code == 200
    assert response.json()["logical_path"] == "/Drive/Archive/drive.pdf"
    assert legacy.move_calls == [("gdrive_1", "/Drive/Archive/drive.pdf")]


# ---------------------------------------------------------------------------
# Managed rename and move
# ---------------------------------------------------------------------------


def test_managed_local_rename_uses_account_adapter(env):
    account = make_account(env, configuration={"storage_root": "/data/a"})
    adapter = _FakeAccountAdapter()
    adapter_manager.install(account.id, adapter)
    file_id = seed_record(env, account_id=account.id, provider_file_id="stored-a.bin")

    response = do_rename(env, file_id, "managed-renamed.txt")

    assert response.status_code == 200
    assert response.json()["name"] == "managed-renamed.txt"
    assert adapter.rename_calls == [("stored-a.bin", "managed-renamed.txt")]


def test_managed_local_move_uses_account_adapter(env):
    account = make_account(env, configuration={"storage_root": "/data/a"})
    adapter = _FakeAccountAdapter()
    adapter_manager.install(account.id, adapter)
    file_id = seed_record(env, account_id=account.id, provider_file_id="stored-a.bin")

    response = do_move(env, file_id, "/Managed/moved.txt")

    assert response.status_code == 200
    assert response.json()["logical_path"] == "/Managed/moved.txt"
    assert adapter.move_calls == [("stored-a.bin", "/Managed/moved.txt")]


def test_managed_google_drive_rename_uses_account_adapter(env):
    account = make_account(
        env, provider_id="GOOGLE_DRIVE", credentials_ref="ref-a"
    )
    adapter = _FakeAccountAdapter()
    adapter_manager.install(account.id, adapter)
    file_id = seed_record(
        env, provider="GOOGLE_DRIVE", account_id=account.id, provider_file_id="gdrive_a"
    )

    response = do_rename(env, file_id, "drive-a-renamed.pdf")

    assert response.status_code == 200
    assert adapter.rename_calls == [("gdrive_a", "drive-a-renamed.pdf")]


def test_managed_google_drive_move_uses_account_adapter(env):
    account = make_account(
        env, provider_id="GOOGLE_DRIVE", credentials_ref="ref-a"
    )
    adapter = _FakeAccountAdapter()
    adapter_manager.install(account.id, adapter)
    file_id = seed_record(
        env, provider="GOOGLE_DRIVE", account_id=account.id, provider_file_id="gdrive_a"
    )

    response = do_move(env, file_id, "/Drive/A/moved.pdf")

    assert response.status_code == 200
    assert adapter.move_calls == [("gdrive_a", "/Drive/A/moved.pdf")]


def test_two_managed_accounts_rename_independently(env):
    account_a = make_account(env, display_name="A", configuration={"storage_root": "/data/a"})
    account_b = make_account(env, display_name="B", configuration={"storage_root": "/data/b"})
    adapter_a = _FakeAccountAdapter()
    adapter_b = _FakeAccountAdapter()
    adapter_manager.install(account_a.id, adapter_a)
    adapter_manager.install(account_b.id, adapter_b)
    file_a = seed_record(env, account_id=account_a.id, provider_file_id="file-a.bin")
    file_b = seed_record(env, account_id=account_b.id, provider_file_id="file-b.bin")

    response_a = do_rename(env, file_a, "a-renamed.txt")
    response_b = do_rename(env, file_b, "b-renamed.txt")

    assert response_a.status_code == 200
    assert response_b.status_code == 200
    assert adapter_a.rename_calls == [("file-a.bin", "a-renamed.txt")]
    assert adapter_b.rename_calls == [("file-b.bin", "b-renamed.txt")]
    assert fetch(env, file_a)["name"] == "a-renamed.txt"
    assert fetch(env, file_b)["name"] == "b-renamed.txt"


def test_two_managed_accounts_move_independently(env):
    account_a = make_account(env, display_name="A", configuration={"storage_root": "/data/a"})
    account_b = make_account(env, display_name="B", configuration={"storage_root": "/data/b"})
    adapter_a = _FakeAccountAdapter()
    adapter_b = _FakeAccountAdapter()
    adapter_manager.install(account_a.id, adapter_a)
    adapter_manager.install(account_b.id, adapter_b)
    file_a = seed_record(env, account_id=account_a.id, provider_file_id="file-a.bin")
    file_b = seed_record(env, account_id=account_b.id, provider_file_id="file-b.bin")

    response_a = do_move(env, file_a, "/A/moved.txt")
    response_b = do_move(env, file_b, "/B/moved.txt")

    assert response_a.status_code == 200
    assert response_b.status_code == 200
    assert adapter_a.move_calls == [("file-a.bin", "/A/moved.txt")]
    assert adapter_b.move_calls == [("file-b.bin", "/B/moved.txt")]
    assert fetch(env, file_a)["logical_path"] == "/A/moved.txt"
    assert fetch(env, file_b)["logical_path"] == "/B/moved.txt"


# ---------------------------------------------------------------------------
# Metadata, activity, and failure atomicity
# ---------------------------------------------------------------------------


def test_rename_updates_only_name(env):
    account = make_account(env, configuration={"storage_root": "/data/a"})
    adapter_manager.install(account.id, _FakeAccountAdapter())
    file_id = seed_record(
        env,
        account_id=account.id,
        provider_file_id="stored-a.bin",
        name="before.txt",
        logical_path="/before.txt",
    )
    before = fetch(env, file_id)

    assert do_rename(env, file_id, "after.txt").status_code == 200

    after = fetch(env, file_id)
    assert after["name"] == "after.txt"
    for field in (
        "logical_path",
        "provider",
        "provider_account_id",
        "provider_file_id",
        "mime_type",
        "size_bytes",
        "sha256",
        "category",
        "tags",
    ):
        assert after[field] == before[field]


def test_move_updates_only_logical_path(env):
    account = make_account(env, configuration={"storage_root": "/data/a"})
    adapter_manager.install(account.id, _FakeAccountAdapter())
    file_id = seed_record(
        env, account_id=account.id, provider_file_id="stored-a.bin", name="file.txt"
    )
    before = fetch(env, file_id)

    assert do_move(env, file_id, "/After/file.txt").status_code == 200

    after = fetch(env, file_id)
    assert after["logical_path"] == "/After/file.txt"
    for field in (
        "name",
        "provider",
        "provider_account_id",
        "provider_file_id",
        "mime_type",
        "size_bytes",
        "sha256",
        "category",
        "tags",
    ):
        assert after[field] == before[field]


def test_rename_records_activity(env):
    account = make_account(env, configuration={"storage_root": "/data/a"})
    adapter_manager.install(account.id, _FakeAccountAdapter())
    file_id = seed_record(
        env, account_id=account.id, provider_file_id="stored-a.bin", name="old.txt"
    )

    assert do_rename(env, file_id, "new.txt").status_code == 200
    activity = env.client.get("/api/v1/activity", params={"action_type": "RENAME"})

    assert activity.status_code == 200
    body = activity.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["file_id"] == file_id
    assert item["file_name"] == "old.txt"
    assert item["provider"] == "LOCAL"
    assert item["details"]["new_name"] == "new.txt"


def test_move_records_activity(env):
    account = make_account(env, configuration={"storage_root": "/data/a"})
    adapter_manager.install(account.id, _FakeAccountAdapter())
    file_id = seed_record(
        env, account_id=account.id, provider_file_id="stored-a.bin", name="file.txt"
    )

    assert do_move(env, file_id, "/New/file.txt").status_code == 200
    activity = env.client.get("/api/v1/activity", params={"action_type": "MOVE"})

    assert activity.status_code == 200
    body = activity.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["file_id"] == file_id
    assert item["details"]["old_logical_path"] == "/file.txt"
    assert item["details"]["new_logical_path"] == "/New/file.txt"


@pytest.mark.parametrize("op", OPERATIONS)
def test_failed_operation_leaves_metadata_unchanged(env, op):
    account = make_account(env, configuration={"storage_root": "/data/a"})
    adapter = _FakeAccountAdapter()
    adapter.rename_error = StorageNotFoundError("stored-a.bin", provider="LOCAL")
    adapter.move_error = StorageNotFoundError("stored-a.bin", provider="LOCAL")
    adapter_manager.install(account.id, adapter)
    file_id = seed_record(
        env, account_id=account.id, provider_file_id="stored-a.bin", name="before.txt"
    )
    before = fetch(env, file_id)

    response = perform(env, op, file_id)

    assert response.status_code == 404
    assert fetch(env, file_id) == before
    activity = env.client.get("/api/v1/activity")
    assert activity.json()["total"] == 0


# ---------------------------------------------------------------------------
# Fail-closed account errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op", OPERATIONS)
def test_unknown_account_returns_404_without_registry(env, op):
    exploding = _ExplodingLocalAdapter(env.uploads_dir)
    register_local(exploding)
    missing = str(uuid.uuid4())
    file_id = seed_record(env, account_id=missing, provider_file_id="stored.bin")

    response = perform(env, op, file_id)

    assert response.status_code == 404
    assert "account" in response.json()["detail"].lower()
    assert exploding.rename_calls == []
    assert exploding.move_calls == []


@pytest.mark.parametrize("op", OPERATIONS)
def test_disconnected_account_returns_503_without_registry(env, op):
    account = make_account(
        env, status="disconnected", configuration={"storage_root": "/data/a"}
    )
    adapter_manager.install(account.id, _FakeAccountAdapter())
    exploding = _ExplodingLocalAdapter(env.uploads_dir)
    register_local(exploding)
    file_id = seed_record(env, account_id=account.id, provider_file_id="stored.bin")

    response = perform(env, op, file_id)

    assert response.status_code == 503
    assert "not available" in response.json()["detail"].lower()
    assert exploding.rename_calls == []
    assert exploding.move_calls == []


@pytest.mark.parametrize("op", OPERATIONS)
def test_provider_mismatch_returns_409_without_registry(env, op):
    account = make_account(env, provider_id="LOCAL", configuration={"storage_root": "/data/a"})
    adapter_manager.install(account.id, _FakeAccountAdapter())
    exploding = _ExplodingDriveAdapter()
    register_drive(exploding)
    file_id = seed_record(
        env, provider="GOOGLE_DRIVE", account_id=account.id, provider_file_id="gdrive_1"
    )

    response = perform(env, op, file_id)

    assert response.status_code == 409
    assert "does not match" in response.json()["detail"].lower()


@pytest.mark.parametrize("op", OPERATIONS)
def test_managed_resolution_failure_never_consults_registry(env, op, monkeypatch):
    # A LOCAL account with no storage_root cannot build an adapter.
    account = make_account(env, configuration={})
    calls: list[str] = []

    def _forbidden(provider):
        calls.append(provider)
        raise AssertionError("legacy registry must not be consulted")

    monkeypatch.setattr(resolution, "lookup", _forbidden)
    file_id = seed_record(env, account_id=account.id, provider_file_id="stored.bin")

    response = perform(env, op, file_id)

    assert response.status_code == 503
    assert calls == []


@pytest.mark.parametrize("op", OPERATIONS)
@pytest.mark.parametrize(
    "error,status",
    [
        (StorageAuthenticationError(detail="bad token", provider="LOCAL"), 401),
        (StoragePermissionError(provider="LOCAL"), 403),
        (StorageUnavailableError(detail="offline", provider="LOCAL"), 503),
    ],
    ids=["auth-401", "permission-403", "unavailable-503"],
)
def test_provider_errors_keep_established_mapping(env, op, error, status):
    account = make_account(env, configuration={"storage_root": "/data/a"})
    adapter = _FakeAccountAdapter()
    adapter.rename_error = error
    adapter.move_error = error
    adapter_manager.install(account.id, adapter)
    file_id = seed_record(env, account_id=account.id, provider_file_id="stored.bin")

    response = perform(env, op, file_id)

    assert response.status_code == status


# ---------------------------------------------------------------------------
# Cross-operation isolation
# ---------------------------------------------------------------------------


def test_managed_file_never_uses_legacy_adapter(env):
    account = make_account(env, configuration={"storage_root": "/data/a"})
    adapter = _FakeAccountAdapter()
    adapter_manager.install(account.id, adapter)
    exploding = _ExplodingLocalAdapter(env.uploads_dir)
    register_local(exploding)
    file_id = seed_record(env, account_id=account.id, provider_file_id="stored-a.bin")

    assert do_rename(env, file_id, "renamed.txt").status_code == 200
    assert do_move(env, file_id, "/Moved/file.txt").status_code == 200

    assert exploding.rename_calls == []
    assert exploding.move_calls == []
    assert adapter.rename_calls == [("stored-a.bin", "renamed.txt")]
    assert adapter.move_calls == [("stored-a.bin", "/Moved/file.txt")]


def test_legacy_null_record_uses_provider_adapter(env):
    legacy = _RecordingDriveAdapter()
    register_drive(legacy)
    file_id = seed_record(
        env, provider="GOOGLE_DRIVE", provider_file_id="gdrive_legacy", name="legacy.pdf"
    )

    assert do_rename(env, file_id, "legacy-renamed.pdf").status_code == 200
    assert do_move(env, file_id, "/Legacy/moved.pdf").status_code == 200

    assert legacy.rename_calls == [("gdrive_legacy", "legacy-renamed.pdf")]
    assert legacy.move_calls == [("gdrive_legacy", "/Legacy/moved.pdf")]


def test_rename_a_does_not_change_b_metadata(env):
    account_a = make_account(env, display_name="A", configuration={"storage_root": "/data/a"})
    account_b = make_account(env, display_name="B", configuration={"storage_root": "/data/b"})
    adapter_manager.install(account_a.id, _FakeAccountAdapter())
    adapter_manager.install(account_b.id, _FakeAccountAdapter())
    file_a = seed_record(env, account_id=account_a.id, name="a.txt", provider_file_id="a.bin")
    file_b = seed_record(env, account_id=account_b.id, name="b.txt", provider_file_id="b.bin")
    before_b = fetch(env, file_b)

    assert do_rename(env, file_a, "a-renamed.txt").status_code == 200

    assert fetch(env, file_b) == before_b


def test_move_a_does_not_change_b_metadata(env):
    account_a = make_account(env, display_name="A", configuration={"storage_root": "/data/a"})
    account_b = make_account(env, display_name="B", configuration={"storage_root": "/data/b"})
    adapter_manager.install(account_a.id, _FakeAccountAdapter())
    adapter_manager.install(account_b.id, _FakeAccountAdapter())
    file_a = seed_record(env, account_id=account_a.id, provider_file_id="a.bin")
    file_b = seed_record(env, account_id=account_b.id, provider_file_id="b.bin")
    before_b = fetch(env, file_b)

    assert do_move(env, file_a, "/A/moved.txt").status_code == 200

    assert fetch(env, file_b) == before_b


def test_account_adapters_remain_distinct_in_cache(env):
    account_a = make_account(env, display_name="A", configuration={"storage_root": "/data/a"})
    account_b = make_account(env, display_name="B", configuration={"storage_root": "/data/b"})
    adapter_a = _FakeAccountAdapter()
    adapter_b = _FakeAccountAdapter()
    adapter_manager.install(account_a.id, adapter_a)
    adapter_manager.install(account_b.id, adapter_b)

    assert adapter_manager.cache[account_a.id] is adapter_a
    assert adapter_manager.cache[account_b.id] is adapter_b
    assert adapter_manager.cache[account_a.id] is not adapter_manager.cache[account_b.id]


def test_invalidating_a_keeps_b_cached(env):
    account_a = make_account(env, display_name="A", configuration={"storage_root": "/data/a"})
    account_b = make_account(env, display_name="B", configuration={"storage_root": "/data/b"})
    adapter_a = _FakeAccountAdapter()
    adapter_b = _FakeAccountAdapter()
    adapter_manager.install(account_a.id, adapter_a)
    adapter_manager.install(account_b.id, adapter_b)

    adapter_manager.invalidate_account(account_a.id)

    assert adapter_manager.cache.get(account_a.id) is None
    assert adapter_manager.cache[account_b.id] is adapter_b
