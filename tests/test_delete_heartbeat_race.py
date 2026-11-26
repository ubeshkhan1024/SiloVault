"""Delete-heartbeat race regression tests (HVSA H1.5.4 Step 7, Part A).

``delete_file`` commits the metadata deletion and the durable
``PendingDeletion`` row, then starts a heartbeat thread that renews the lease
while ``adapter.cleanup()`` runs.  ``db.commit()`` expires the ORM instance, so
the heartbeat thread must never read attributes from it: doing so issues a
refresh on the request's session from another thread and raises
``ObjectDeletedError``.

These tests make the heartbeat win the race deterministically (``cleanup``
sleeps past the first heartbeat tick) and prove that:

* a managed LOCAL delete succeeds and finalizes the row,
* a failed cleanup still schedules the retry (row kept, ownership released),
* the heartbeat UPDATE targets the captured scalar row id,
* no ``ObjectDeletedError`` is raised or logged,
* the legacy (NULL account) delete path is unchanged.

Isolated in-memory database, fake adapters and a temporary directory only.
"""

from __future__ import annotations

import logging
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import settings
from backend.database import Base, get_db
from backend.main import app
from backend.models.pending_deletion import PendingDeletion
from backend.models.storage_account import StorageAccount
from backend.models.unified_file import UnifiedFileRecord
from backend.storage.account_adapter_manager import adapter_manager
from backend.storage.capabilities import CAPABILITY_MAP
from backend.storage.errors import StorageUnavailableError
from backend.storage.interfaces import StorageOperations
from backend.storage.local import LocalAdapter
from backend.storage.registry import _adapter_map, _capability_map, register
from backend.storage.types import ProviderFileRecord

#: Longer than one heartbeat tick (the loop renews immediately, then waits 1 s).
HEARTBEAT_WINDOW_SECONDS = 1.2


class _HeartbeatAwareAdapter(StorageOperations):
    """Account adapter whose cleanup keeps the heartbeat thread alive."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.quarantine_calls: list[str] = []
        self.cleanup_calls: list[str] = []
        #: ``"cleanup"`` while ``cleanup()`` runs, ``"after"`` once it returns.
        #: Lets a SQL listener attribute statements to the heartbeat window.
        self.phase = "before"

    def upload(self, file_data, name, logical_path) -> ProviderFileRecord:
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
        self.quarantine_calls.append(provider_file_id)
        return f"q::{provider_file_id}"

    def restore(self, quarantine_id: str) -> ProviderFileRecord:
        raise NotImplementedError

    def cleanup(self, quarantine_id: str) -> None:
        self.cleanup_calls.append(quarantine_id)
        self.phase = "cleanup"
        try:
            # Let the heartbeat thread run at least one renewal cycle before
            # the request thread proceeds, which is what exposes cross-thread
            # ORM use.
            time.sleep(HEARTBEAT_WINDOW_SECONDS)
            if self.error is not None:
                raise self.error
        finally:
            self.phase = "after"


@pytest.fixture
def env(tmp_path, monkeypatch):
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


def seed_account(env, provider_id: str = "LOCAL") -> str:
    session = env.Session()
    try:
        account = StorageAccount(
            id=str(uuid.uuid4()),
            provider_id=provider_id,
            display_name=f"{provider_id} account",
            configuration={},
            status="connected",
        )
        session.add(account)
        session.commit()
        return account.id
    finally:
        session.close()


def seed_record(
    env, *, account_id: str | None, provider: str = "LOCAL"
) -> str:
    session = env.Session()
    try:
        record = UnifiedFileRecord(
            provider=provider,
            provider_account_id=account_id,
            provider_file_id="stored.bin",
            name="stored.bin",
            logical_path="/stored.bin",
            mime_type="application/octet-stream",
            size_bytes=1,
            sync_state="LOCAL" if provider == "LOCAL" else "COMPLETED",
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record.id
    finally:
        session.close()


def pending_rows(env) -> list[PendingDeletion]:
    session = env.Session()
    try:
        return list(session.execute(select(PendingDeletion)).scalars().all())
    finally:
        session.close()


def record_exists(env, file_id: str) -> bool:
    session = env.Session()
    try:
        return session.get(UnifiedFileRecord, file_id) is not None
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Managed LOCAL delete
# ---------------------------------------------------------------------------


def test_managed_local_delete_finalizes_after_heartbeat_tick(env, caplog):
    caplog.set_level(logging.WARNING)

    account_id = seed_account(env)
    adapter = _HeartbeatAwareAdapter()
    adapter_manager.install(account_id, adapter)
    file_id = seed_record(env, account_id=account_id)

    response = env.client.delete(f"/api/v1/files/{file_id}")

    assert response.status_code == 204
    assert adapter.quarantine_calls == ["stored.bin"]
    assert adapter.cleanup_calls == ["q::stored.bin"]
    assert pending_rows(env) == []
    assert record_exists(env, file_id) is False
    assert "ObjectDeletedError" not in caplog.text


def test_failed_cleanup_after_heartbeat_tick_schedules_retry(env, caplog):
    caplog.set_level(logging.WARNING)

    account_id = seed_account(env)
    adapter = _HeartbeatAwareAdapter(
        error=StorageUnavailableError("cleanup failed", provider="LOCAL")
    )
    adapter_manager.install(account_id, adapter)
    file_id = seed_record(env, account_id=account_id)

    response = env.client.delete(f"/api/v1/files/{file_id}")

    assert response.status_code == 204
    rows = pending_rows(env)
    assert len(rows) == 1
    row = rows[0]
    # The retry branch ran: the row was handed back to recovery, which is only
    # reachable when the heartbeat did not fail with a stale-instance error.
    assert row.status == "PENDING"
    assert row.retry_count == 1
    assert row.claim_token is None
    assert row.next_retry_at is not None
    assert row.provider_account_id == account_id
    assert record_exists(env, file_id) is False
    assert "ObjectDeletedError" not in caplog.text


def test_heartbeat_update_uses_captured_row_id(env):
    from sqlalchemy import event

    executed: list[tuple[str, str, object]] = []

    account_id = seed_account(env)
    adapter = _HeartbeatAwareAdapter(
        error=StorageUnavailableError("cleanup failed", provider="LOCAL")
    )
    adapter_manager.install(account_id, adapter)
    file_id = seed_record(env, account_id=account_id)

    @event.listens_for(env.engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        executed.append((adapter.phase, statement, parameters))

    response = env.client.delete(f"/api/v1/files/{file_id}")

    assert response.status_code == 204
    rows = pending_rows(env)
    assert len(rows) == 1
    pending_id = rows[0].id

    def _params(parameters) -> set:
        if isinstance(parameters, dict):
            return set(parameters.values())
        return set(parameters or ())

    # While cleanup() was running, only the heartbeat thread can issue SQL. The
    # lease renewal must therefore bind the captured scalar id.
    heartbeat_updates = [
        (statement, parameters)
        for phase, statement, parameters in executed
        if phase == "cleanup"
        and statement.upper().lstrip().startswith("UPDATE PENDING_DELETIONS")
    ]
    assert heartbeat_updates, "heartbeat issued no lease-renewal UPDATE"
    assert any(pending_id in _params(parameters) for _, parameters in heartbeat_updates)


# ---------------------------------------------------------------------------
# Legacy (NULL account) delete path
# ---------------------------------------------------------------------------


def test_legacy_local_delete_finalizes_after_heartbeat_tick(env, caplog):
    caplog.set_level(logging.WARNING)

    _adapter_map.pop("LOCAL", None)
    _capability_map.pop("LOCAL", None)
    register("LOCAL", LocalAdapter(str(env.uploads_dir)), CAPABILITY_MAP["LOCAL"])
    (env.uploads_dir / "stored.bin").write_bytes(b"legacy")
    file_id = seed_record(env, account_id=None)

    response = env.client.delete(f"/api/v1/files/{file_id}")

    assert response.status_code == 204
    assert pending_rows(env) == []
    assert record_exists(env, file_id) is False
    assert "ObjectDeletedError" not in caplog.text
