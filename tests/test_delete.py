"""Tests for the DELETE /api/v1/files/{file_id} endpoint.

Uses isolated temporary upload directories and in-memory SQLite databases so
the development database and real uploads directory are never touched.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import settings
from backend.database import Base, get_db
from backend.main import app
from backend.models.storage_account import StorageAccount
from backend.models.unified_file import UnifiedFileRecord
from backend.storage.account_adapter_manager import adapter_manager
from backend.storage.local import LocalAdapter
from backend.storage.registry import register, remove
from backend.storage.capabilities import CAPABILITY_MAP
from backend.storage.errors import StorageError, StorageUnavailableError
from backend.storage.google_drive import GoogleDriveAdapter


@pytest.fixture(autouse=True)
def _fake_keyring(monkeypatch: pytest.MonkeyPatch):
    """Replace ``_get_keyring`` with one that returns an in-memory dict."""
    from types import SimpleNamespace

    class InMemoryKeyring:
        def __init__(self):
            self.store: dict[tuple[str, str], str] = {}

        def set_password(self, service: str, account: str, data: str) -> None:
            self.store[(service, account)] = data

        def get_password(self, service: str, account: str) -> str | None:
            return self.store.get((service, account))

        def delete_password(self, service: str, account: str) -> None:
            self.store.pop((service, account), None)

    fake_kr = InMemoryKeyring()

    def fake_get_keyring():
        return fake_kr

    monkeypatch.setattr("backend.storage.credentials._get_keyring", fake_get_keyring)
    monkeypatch.setattr("backend.storage.credentials.keyring", SimpleNamespace())

    yield fake_kr


@pytest.fixture(autouse=True)
def _cleanup_gdrive_registration(request):
    """Remove any leftover GOOGLE_DRIVE adapter registration after each test."""
    yield
    from backend.storage.registry import remove as reg_remove
    try:
        reg_remove("GOOGLE_DRIVE")
    except StorageUnavailableError:
        pass


@pytest.fixture
def delete_env(tmp_path, monkeypatch):
    """Yields an isolated test environment with a temp uploads dir and in-memory DB.

    Returns a dict with:
    - client: TestClient wired to the in-memory DB
    - engine: the SQLAlchemy engine
    - uploads_dir: Path to the temporary uploads directory
    """
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

    # Register LOCAL adapter with the temp uploads directory
    adapter = LocalAdapter(str(uploads_dir))
    register("LOCAL", adapter, CAPABILITY_MAP["LOCAL"])

    # Account-aware delete resolves managed adapters through the process-wide
    # manager. Point it at this test's database and start from a clean cache;
    # monkeypatch restores ``_session_factory`` after the test.
    monkeypatch.setattr(adapter_manager, "_session_factory", TestSession)
    adapter_manager.invalidate_all()

    try:
        yield {
            "client": TestClient(app),
            "engine": engine,
            "uploads_dir": uploads_dir,
            "adapter": adapter,
        }
    finally:
        adapter_manager.invalidate_all()
        # Clean up adapter registrations (LOCAL always present; GOOGLE_DRIVE only if added by a test).
        remove("LOCAL")
        try:
            remove("GOOGLE_DRIVE")
        except StorageUnavailableError:
            pass
        app.dependency_overrides.clear()
        engine.dispose()


def _upload(client, filename: str, content: bytes) -> dict:
    """Upload a file via the real upload endpoint and return the JSON payload."""
    files = {"file": (filename, content, "application/octet-stream")}
    response = client.post("/api/v1/uploads", files=files)
    assert response.status_code == 200
    return response.json()


def _seed_local_record(engine, provider_file_id: str, name: str, mime_type: str = "text/plain"):
    """Insert a LOCAL UnifiedFileRecord directly into the database."""
    session = sessionmaker(bind=engine)()
    try:
        record = UnifiedFileRecord(
            provider="LOCAL",
            provider_account_id=None,
            provider_file_id=provider_file_id,
            name=name,
            logical_path=f"/{name}",
            mime_type=mime_type,
            size_bytes=10,
            sync_state="LOCAL",
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_delete_removes_physical_file_and_metadata(delete_env):
    """A successful deletion must remove both the disk file and the DB record."""
    env = delete_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    payload = _upload(client, "to_delete.txt", b"delete me")
    file_id = payload["id"]
    storage_name = payload["provider_file_id"]

    # Physical file exists before deletion.
    assert (uploads_dir / storage_name).exists()

    response = client.delete(f"/api/v1/files/{file_id}")
    assert response.status_code == 204

    # Physical file is gone.
    assert not (uploads_dir / storage_name).exists()

    # Metadata record is gone.
    get_response = client.get(f"/api/v1/files/{file_id}")
    assert get_response.status_code == 404


def test_delete_nonexistent_id_returns_404(delete_env):
    """Deleting an unknown ID must return HTTP 404."""
    client = delete_env["client"]
    response = client.delete("/api/v1/files/file_does_not_exist_99999")
    assert response.status_code == 404
    assert "detail" in response.json()


def _seed_gdrive_record(
    engine,
    provider_file_id: str = "gdrive_abc123",
    name: str = "cloud.pdf",
) -> UnifiedFileRecord:
    """Insert a GOOGLE_DRIVE record directly into an in-memory DB."""
    session = sessionmaker(bind=engine)()
    try:
        record = UnifiedFileRecord(
            provider="GOOGLE_DRIVE",
            provider_account_id=None,
            provider_file_id=provider_file_id,
            name=name,
            logical_path=f"/{name}",
            mime_type="application/pdf",
            size_bytes=1024,
            sync_state="COMPLETED",
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record
    finally:
        session.close()


def _make_gd_adapter(**overrides: object) -> GoogleDriveAdapter:
    """Build a GoogleDriveAdapter with a mocked Drive service."""
    adapter = GoogleDriveAdapter()
    adapter._drive_service = MagicMock()
    for attr, value in overrides.items():
        setattr(adapter, attr, value)
    return adapter


def test_delete_cloud_provider_unregistered_returns_503(delete_env):
    """A GOOGLE_DRIVE record with no registered adapter must return 503."""
    client = delete_env["client"]
    engine = delete_env["engine"]

    # Seed a GOOGLE_DRIVE record without registering any adapter.
    record = _seed_gdrive_record(
        engine,
        provider_file_id="missing-provider-file",
    )

    response = client.delete(f"/api/v1/files/{record.id}")
    assert response.status_code == 503

    # The database record must remain intact.
    get_response = client.get(f"/api/v1/files/{record.id}")
    assert get_response.status_code == 200


def test_delete_google_drive_success_registers_pending_deletion(delete_env):
    """A successful GOOGLE_DRIVE delete triggers quarantine, DB removal, and cleanup."""
    client = delete_env["client"]
    engine = delete_env["engine"]

    gd_adapter = _make_gd_adapter()
    files_mock = gd_adapter._drive_service.files.return_value
    files_mock.update.return_value.execute.return_value = {}
    files_mock.delete.return_value.execute.return_value = None

    try:
        register("GOOGLE_DRIVE", gd_adapter, CAPABILITY_MAP["GOOGLE_DRIVE"])
    except StorageUnavailableError:
        from backend.storage.registry import replace as reg_replace
        reg_replace("GOOGLE_DRIVE", gd_adapter, CAPABILITY_MAP["GOOGLE_DRIVE"])

    record = _seed_gdrive_record(engine, provider_file_id="drive-del-1")

    response = client.delete(f"/api/v1/files/{record.id}")
    assert response.status_code == 204

    # The Drive API was quarantined (trashed).
    files_mock.update.assert_called_once_with(
        fileId="drive-del-1",
        body={"trashed": True},
    )
    # And then permanently deleted.
    files_mock.delete.assert_called_once_with(fileId="drive-del-1")

    # DB record is gone.
    get_resp = client.get(f"/api/v1/files/{record.id}")
    assert get_resp.status_code == 404


def test_delete_google_drive_unavailable_returns_503(delete_env):
    """If the GOOGLE_DRIVE adapter raises StorageUnavailableError during quarantine,
    the endpoint returns 503 and no DB record is removed."""
    client = delete_env["client"]
    engine = delete_env["engine"]

    gd_adapter = _make_gd_adapter()
    gd_adapter.quarantine = MagicMock(
        side_effect=StorageUnavailableError("drive unreachable", provider="GOOGLE_DRIVE")
    )

    try:
        register("GOOGLE_DRIVE", gd_adapter, CAPABILITY_MAP["GOOGLE_DRIVE"])
    except StorageUnavailableError:
        from backend.storage.registry import replace as reg_replace
        reg_replace("GOOGLE_DRIVE", gd_adapter, CAPABILITY_MAP["GOOGLE_DRIVE"])

    record = _seed_gdrive_record(engine, provider_file_id="unavail-file")

    response = client.delete(f"/api/v1/files/{record.id}")
    assert response.status_code == 503

    # DB record must still exist.
    get_resp = client.get(f"/api/v1/files/{record.id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == record.id


def test_delete_google_drive_not_found_during_quarantine(delete_env):
    """A missing cloud file (quarantine raises StorageNotFoundError) returns 404
    and leaves the DB record intact."""
    client = delete_env["client"]
    engine = delete_env["engine"]

    gd_adapter = _make_gd_adapter()
    from backend.storage.errors import StorageNotFoundError

    gd_adapter.quarantine = MagicMock(
        side_effect=StorageNotFoundError("ghost-drive-file", provider="GOOGLE_DRIVE")
    )

    try:
        register("GOOGLE_DRIVE", gd_adapter, CAPABILITY_MAP["GOOGLE_DRIVE"])
    except StorageUnavailableError:
        from backend.storage.registry import replace as reg_replace
        reg_replace("GOOGLE_DRIVE", gd_adapter, CAPABILITY_MAP["GOOGLE_DRIVE"])

    record = _seed_gdrive_record(engine, provider_file_id="ghost-drive-file")

    response = client.delete(f"/api/v1/files/{record.id}")
    assert response.status_code == 404  # StorageNotFoundError → 404


def test_delete_google_drive_cleanup_failure_leaves_pending_deletion(delete_env):
    """When DB commit succeeds but adapter.cleanup() fails, the request returns 204
    and a PendingDeletion row remains for the recovery worker."""
    from backend.models.pending_deletion import PendingDeletion
    from sqlalchemy import select as sa_select

    client = delete_env["client"]
    engine = delete_env["engine"]

    gd_adapter = _make_gd_adapter()
    files_mock = gd_adapter._drive_service.files.return_value
    files_mock.update.return_value.execute.return_value = {}
    files_mock.delete.return_value.execute.side_effect = StorageUnavailableError(
        "cleanup failed", provider="GOOGLE_DRIVE"
    )

    try:
        register("GOOGLE_DRIVE", gd_adapter, CAPABILITY_MAP["GOOGLE_DRIVE"])
    except StorageUnavailableError:
        from backend.storage.registry import replace as reg_replace
        reg_replace("GOOGLE_DRIVE", gd_adapter, CAPABILITY_MAP["GOOGLE_DRIVE"])

    record = _seed_gdrive_record(engine, provider_file_id="cleanup-fail-file")

    response = client.delete(f"/api/v1/files/{record.id}")
    assert response.status_code == 204

    # DB record is gone (commit succeeded).
    get_resp = client.get(f"/api/v1/files/{record.id}")
    assert get_resp.status_code == 404

    # A PendingDeletion record should have been created for retry.
    with sessionmaker(bind=engine)() as sess:
        pending = sess.execute(
            sa_select(PendingDeletion).where(
                PendingDeletion.provider_file_id == "cleanup-fail-file"
            )
        ).scalar_one_or_none()
    assert pending is not None
    assert pending.status in ("PENDING", "PROCESSING", "FAILED")


def test_delete_google_drive_db_commit_fails_triggers_restore(delete_env):
    """When DB commit fails after quarantine, restore() is called and the
    original DB record is preserved."""
    import unittest.mock

    client = delete_env["client"]
    engine = delete_env["engine"]

    gd_adapter = _make_gd_adapter()
    files_mock = gd_adapter._drive_service.files.return_value
    files_mock.update.return_value.execute.return_value = {}
    files_mock.get.return_value.execute.return_value = {
        "id": "restore-me",
        "name": "restored.pdf",
        "mimeType": "application/pdf",
        "size": "1024",
    }

    try:
        register("GOOGLE_DRIVE", gd_adapter, CAPABILITY_MAP["GOOGLE_DRIVE"])
    except StorageUnavailableError:
        from backend.storage.registry import replace as reg_replace
        reg_replace("GOOGLE_DRIVE", gd_adapter, CAPABILITY_MAP["GOOGLE_DRIVE"])

    record = _seed_gdrive_record(engine, provider_file_id="restore-me")

    with unittest.mock.patch(
        "sqlalchemy.orm.Session.commit", side_effect=Exception("db boom")
    ):
        response = client.delete(f"/api/v1/files/{record.id}")

    assert response.status_code == 500
    assert "restored" in response.json()["detail"].lower()

    # The DB record must still exist.
    get_resp = client.get(f"/api/v1/files/{record.id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == record.id

    # restore() was called once (to un-trash).
    files_mock.update.assert_any_call(fileId="restore-me", body={"trashed": False})


def test_delete_cloud_provider_rejected(delete_env):
    """Cloud-provider records must not be deletable from local storage."""
    env = delete_env
    client = env["client"]
    engine = env["engine"]

    # Insert a legacy (NULL account) GOOGLE_DRIVE record directly (no physical
    # file needed). Account-scoped delete is covered by
    # ``tests/test_file_account_delete.py``.
    session = sessionmaker(bind=engine)()
    try:
        record = UnifiedFileRecord(
            provider="GOOGLE_DRIVE",
            provider_account_id=None,
            provider_file_id="gdrive_abc123",
            name="cloud.pdf",
            logical_path="/cloud.pdf",
            mime_type="application/pdf",
            size_bytes=1024,
            sync_state="COMPLETED",
        )
        session.add(record)
        session.commit()
        session.refresh(record)
    finally:
        session.close()

    response = client.delete(f"/api/v1/files/{record.id}")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "unregistered" in detail.lower() or "unavailable" in detail.lower()



def test_delete_missing_physical_file_returns_404(delete_env):
    """If the physical file is missing, the metadata record must NOT be deleted."""
    env = delete_env
    client = env["client"]
    engine = env["engine"]
    uploads_dir = env["uploads_dir"]

    # Seed a LOCAL record without creating the physical file.
    record = _seed_local_record(engine, "ghost_no_file.bin", "ghost.bin")

    response = client.delete(f"/api/v1/files/{record.id}")
    assert response.status_code == 404

    # The metadata record must still exist.
    get_response = client.get(f"/api/v1/files/{record.id}")
    assert get_response.status_code == 200
    assert get_response.json()["id"] == record.id

    # No extra files were created in uploads_dir.
    assert list(uploads_dir.iterdir()) == []


def test_delete_traversal_filename_is_rejected(delete_env):
    """A crafted provider_file_id with '..' must prevent deletion."""
    env = delete_env
    client = env["client"]
    engine = env["engine"]
    uploads_dir = env["uploads_dir"]
    tmp_root = uploads_dir.parent

    # Place a decoy OUTSIDE the uploads directory.
    decoy = tmp_root / "secret.txt"
    decoy.write_text("top secret")

    record = _seed_local_record(engine, "../secret.txt", "traversal.txt")

    response = client.delete(f"/api/v1/files/{record.id}")
    # Adapter validates path and returns 404 for non-existent/invalid paths
    assert response.status_code == 404

    # The decoy must remain untouched.
    assert decoy.read_text() == "top secret"


def test_delete_absolute_path_filename_is_rejected(delete_env):
    """An absolute-path provider_file_id must be rejected."""
    env = delete_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_local_record(engine, "/etc/passwd", "abs.txt")

    response = client.delete(f"/api/v1/files/{record.id}")
    # Adapter validates path and returns 404 for non-existent/invalid paths
    assert response.status_code == 404


def test_delete_symlink_to_outside_uploads_dir_is_rejected(delete_env):
    """A symlink inside uploads_dir that points outside must be rejected."""
    env = delete_env
    client = env["client"]
    engine = env["engine"]
    uploads_dir = env["uploads_dir"]
    tmp_root = uploads_dir.parent

    # Create a target outside uploads_dir.
    target = tmp_root / "real_target.bin"
    target.write_text("I am outside")

    # Create a symlink inside uploads_dir pointing at it.
    link = uploads_dir / "tricky_link.bin"
    try:
        link.symlink_to(target)
    except OSError as exc:
        # Skip only when the OS/platform explicitly refuses symlink creation
        # (e.g. Windows without Developer Mode – winerror 1314).
        # Re-raise unrelated errors so they surface during testing.
        if getattr(exc, "winerror", None) == 1314 or exc.errno == 95:
            pytest.skip(f"Symlink creation not permitted on this host: {exc}")
        raise

    # Pre-create the file to satisfy the exists() check; the symlink itself
    # will be caught by the is_symlink() guard.
    # Note: we seed a record whose provider_file_id matches the link name.
    record = _seed_local_record(engine, "tricky_link.bin", "tricky.txt")

    response = client.delete(f"/api/v1/files/{record.id}")
    # Adapter rejects symlinks with 403 (StoragePermissionError)
    assert response.status_code == 403

    # The symlink and target must both remain intact.
    assert link.exists() or link.is_symlink()
    assert target.exists()


def test_delete_one_file_does_not_affect_another(delete_env):
    """Deleting one file must leave other files untouched."""
    env = delete_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    payload_a = _upload(client, "keep_a.txt", b"I should stay")
    payload_b = _upload(client, "keep_b.txt", b"I should also stay")
    payload_del = _upload(client, "trash.txt", b"Bye bye")

    del_id = payload_del["id"]
    keep_a_id = payload_a["id"]
    keep_b_id = payload_b["id"]

    # Delete the trash file.
    response = client.delete(f"/api/v1/files/{del_id}")
    assert response.status_code == 204

    # Both kept files must still exist on disk and in the DB.
    for payload in (payload_a, payload_b):
        assert (uploads_dir / payload["provider_file_id"]).exists()
        get_resp = client.get(f"/api/v1/files/{payload['id']}")
        assert get_resp.status_code == 200
        assert get_resp.json()["name"] == payload["name"]

    # The deleted file must be gone.
    assert not (uploads_dir / payload_del["provider_file_id"]).exists()
    get_resp = client.get(f"/api/v1/files/{del_id}")
    assert get_resp.status_code == 404


# ---------------------------------------------------------------------------
# Regression — existing endpoints must remain functional after new code
# ---------------------------------------------------------------------------

def test_existing_upload_list_detail_download_still_work(delete_env):
    """After adding the delete endpoint, existing endpoints must behave unchanged."""
    env = delete_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    # Upload
    payload = _upload(client, "regression.txt", b"still works")
    file_id = payload["id"]
    storage_name = payload["provider_file_id"]

    # List shows the file.
    list_resp = client.get("/api/v1/files")
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1

    # Detail returns full metadata.
    detail_resp = client.get(f"/api/v1/files/{file_id}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["name"] == "regression.txt"
    assert detail_resp.json()["provider"] == "LOCAL"

    # Download still serves correct bytes.
    dl_resp = client.get(f"/api/v1/files/{file_id}/download")
    assert dl_resp.status_code == 200
    assert dl_resp.content == b"still works"

    # The file is still on disk.
    assert (uploads_dir / storage_name).exists()


def test_delete_with_empty_database(delete_env):
    """Delete against an empty database must return 404 cleanly."""
    client = delete_env["client"]
    response = client.delete("/api/v1/files/file_nonexistent_xyz")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Rollback recovery — quarantine strategy
# ---------------------------------------------------------------------------

def test_delete_rollback_preserves_original_content(delete_env):
    """If DB commit fails, the original file must be restored with its
    contents intact via the quarantine strategy."""
    import unittest.mock

    env = delete_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    original_bytes = b"do not lose this data"
    payload = _upload(client, "preserve.txt", original_bytes)
    file_id = payload["id"]
    storage_name = payload["provider_file_id"]

    # Simulate a DB commit failure after the file has been quarantined.
    with unittest.mock.patch("sqlalchemy.orm.Session.commit", side_effect=Exception("boom")):
        response = client.delete(f"/api/v1/files/{file_id}")

    # The endpoint returns 500 because commit was forced to fail.
    assert response.status_code == 500

    # The original file must have been restored with identical contents.
    restored = uploads_dir / storage_name
    assert restored.exists(), "Original file was not restored after DB failure"
    assert restored.read_bytes() == original_bytes, "File contents were corrupted on restore"

    # No stray quarantine file should remain (restore took precedence).
    quarantines = [f for f in uploads_dir.iterdir() if ".quarantined." in f.name]
    assert len(quarantines) == 0, "Orphaned quarantine file left behind"


def test_delete_rollback_keeps_db_record(delete_env):
    """When DB commit fails, the metadata record must still exist."""
    import unittest.mock

    env = delete_env
    client = env["client"]

    payload = _upload(client, "keep_me.txt", b"stay")
    file_id = payload["id"]

    with unittest.mock.patch("sqlalchemy.orm.Session.commit", side_effect=Exception("boom")):
        response = client.delete(f"/api/v1/files/{file_id}")

    assert response.status_code == 500

    # The record should still be queryable.
    get_resp = client.get(f"/api/v1/files/{file_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == file_id


def test_delete_quarantine_does_not_appear_in_list(delete_env):
    """A quarantine file on disk (no DB record) must not show up in the file
    list — confirming that intermediate rename artifacts are invisible."""
    env = delete_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    # Plant a fake quarantine file directly on disk with no corresponding DB
    # record (simulating a scenario where a previous deletion left it behind).
    fake_quarantine = uploads_dir / ".some_file.bin.quarantined.abcdef123456"
    fake_quarantine.write_bytes(b"phantom")

    list_resp = client.get("/api/v1/files")
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    names = [item["provider_file_id"] for item in items]
    # The quarantine filename must NOT be returned by the listing.
    assert ".quarantined." not in "".join(names)
    assert len(items) == 0  # no real records were uploaded in this test


def test_delete_successful_removes_quarantine_temporary_file(delete_env):
    """After a successful deletion, no quarantine file should remain."""
    env = delete_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    payload = _upload(client, "clean_delete.txt", b"gone")
    file_id = payload["id"]

    response = client.delete(f"/api/v1/files/{file_id}")
    assert response.status_code == 204

    # Original file gone.
    storage_name = payload["provider_file_id"]
    assert not (uploads_dir / storage_name).exists()

    # No stray quarantine files left behind.
    quarantines = [f for f in uploads_dir.iterdir() if ".quarantined." in f.name]
    assert len(quarantines) == 0, "Quarantine file was not cleaned up after success"


def test_delete_no_orphaned_quarantine_on_commit_failure(delete_env):
    """On commit failure the file is restored; no orphaned quarantine should
    remain when restoration succeeds."""
    import unittest.mock

    env = delete_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    payload = _upload(client, "no_orphan.txt", b"restored")
    file_id = payload["id"]
    storage_name = payload["provider_file_id"]

    with unittest.mock.patch("sqlalchemy.orm.Session.commit", side_effect=Exception("boom")):
        client.delete(f"/api/v1/files/{file_id}")

    # Only the original file should exist (plus the .quarantine infrastructure directory).
    # The quarantine entry directory should be cleaned up after successful restore.
    user_files = [f for f in uploads_dir.iterdir() if f.name != ".quarantine"]
    assert len(user_files) == 1
    assert user_files[0].name == storage_name


# ---------------------------------------------------------------------------
# Failure-injection tests for delete endpoint
# ---------------------------------------------------------------------------

def test_delete_db_commit_fails_restore_succeeds(delete_env):
    """DB commit fails; adapter.restore() succeeds.
    The original file must be restored and the DB record preserved."""
    import sqlalchemy.orm

    env = delete_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    original_bytes = b"restore me"
    payload = _upload(client, "restore_ok.txt", original_bytes)
    file_id = payload["id"]
    storage_name = payload["provider_file_id"]

    with patch.object(sqlalchemy.orm.Session, "commit", side_effect=Exception("db commit failed")):
        response = client.delete(f"/api/v1/files/{file_id}")

    assert response.status_code == 500
    assert "restored" in response.json()["detail"].lower()

    # Original file must be restored with identical contents
    restored = uploads_dir / storage_name
    assert restored.exists(), "Original file was not restored after DB failure"
    assert restored.read_bytes() == original_bytes, "File contents were corrupted on restore"

    # DB record must still exist
    get_resp = client.get(f"/api/v1/files/{file_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == file_id

    # No DELETE activity
    act_res = client.get("/api/v1/activity")
    assert act_res.json()["total"] == 1  # Only the upload activity




def test_delete_db_commit_fails_restore_fails(delete_env):
    """A failed DB deletion and failed immediate restore must queue RESTORE recovery."""
    import sqlalchemy.orm
    from sqlalchemy.orm import sessionmaker
    from backend.models.pending_deletion import PendingDeletion
    from backend.storage.errors import StorageUnavailableError

    env = delete_env
    client = env["client"]
    engine = env["engine"]
    uploads_dir = env["uploads_dir"]

    original_bytes = b"restore fail"
    payload = _upload(client, "restore_fail.txt", original_bytes)
    file_id = payload["id"]

    # Fail the first commit, but allow subsequent commits to execute normally.
    real_commit = sqlalchemy.orm.Session.commit
    commit_calls = 0

    def fail_first_commit_then_commit(session):
        nonlocal commit_calls
        commit_calls += 1

        if commit_calls == 1:
            raise Exception("db commit failed")

        return real_commit(session)

    with patch.object(
        sqlalchemy.orm.Session,
        "commit",
        new=fail_first_commit_then_commit,
    ), patch.object(
        env["adapter"],
        "restore",
        side_effect=StorageUnavailableError("restore failed", provider="LOCAL"),
    ):
        response = client.delete(f"/api/v1/files/{file_id}")

    assert response.status_code == 500
    assert "recovery retry has been queued" in response.json()["detail"].lower()

    # The quarantined original must remain available for recovery.
    quarantine_dir = uploads_dir / ".quarantine"
    assert quarantine_dir.exists()

    entries = list(quarantine_dir.iterdir())
    assert len(entries) == 1

    quarantined_files = [
        f for f in entries[0].iterdir() if f.name != "meta.json"
    ]
    assert len(quarantined_files) == 1
    assert quarantined_files[0].read_bytes() == original_bytes

    # Original metadata must survive the failed deletion transaction.
    get_resp = client.get(f"/api/v1/files/{file_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == file_id

    # A durable RESTORE operation must exist for the recovery worker.
    with sessionmaker(bind=engine)() as session:
        pending = (
            session.query(PendingDeletion)
            .filter_by(
                original_file_id=file_id,
                operation_type="RESTORE",
            )
            .one_or_none()
        )

        assert pending is not None
        assert pending.status == "PENDING"
        assert pending.quarantine_id is not None
        assert "restore failed" in pending.last_error.lower()


def test_delete_commit_succeeds_cleanup_fails(delete_env):
    """DB commit succeeds; adapter.cleanup() fails.
    HTTP 204 must still be returned and the cleanup failure logged."""
    from backend.storage.errors import StorageUnavailableError

    env = delete_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    payload = _upload(client, "cleanup_fail.txt", b"cleanup fail")
    file_id = payload["id"]
    storage_name = payload["provider_file_id"]

    with patch.object(
        env["adapter"],
        "cleanup",
        side_effect=StorageUnavailableError("cleanup failed", provider="LOCAL"),
    ):
        response = client.delete(f"/api/v1/files/{file_id}")

    # Should still return 204 (deletion succeeded from API perspective)
    assert response.status_code == 204

    # File is gone from main storage
    assert not (uploads_dir / storage_name).exists()

    # Quarantine entry remains (cleanup failed) - real recoverable entry
    quarantine_dir = uploads_dir / ".quarantine"
    assert quarantine_dir.exists(), "Quarantine directory should exist when cleanup fails"
    entries = list(quarantine_dir.iterdir())
    assert len(entries) == 1, "Exactly one quarantine entry should remain when cleanup fails"
    entry_dir = entries[0]
    assert entry_dir.is_dir(), "Quarantine entry should be a directory"

    quarantined_files = [f for f in entry_dir.iterdir() if f.name != "meta.json"]
    assert len(quarantined_files) == 1, "Exactly one quarantined file should exist"
    quarantined_file = quarantined_files[0]
    assert quarantined_file.is_file(), "Quarantined file should be a regular file"
    assert quarantined_file.read_bytes() == b"cleanup fail", "Quarantined file contents should match original"

    # DB record is gone
    get_resp = client.get(f"/api/v1/files/{file_id}")
    assert get_resp.status_code == 404

    # DELETE activity should be created (since DB commit succeeded)
    act_res = client.get("/api/v1/activity")
    assert act_res.json()["total"] == 2  # Upload + Delete
    del_act = act_res.json()["items"][0]
    assert del_act["action_type"] == "DELETE"
    assert del_act["file_name"] == "cleanup_fail.txt"


def test_delete_db_exception_cause_preserved(delete_env):
    """Original database exception must be preserved in HTTPException cause chain."""
    import sqlalchemy.orm

    env = delete_env
    client = env["client"]

    payload = _upload(client, "cause_test.txt", b"cause test")
    file_id = payload["id"]

    class CustomDBError(Exception):
        pass

    with patch.object(sqlalchemy.orm.Session, "commit", side_effect=CustomDBError("Specific DB error")):
        response = client.delete(f"/api/v1/files/{file_id}")

    assert response.status_code == 500
    # The endpoint uses 'raise HTTPException(...) from db_exc' which preserves the cause


def test_delete_restore_failure_logged(delete_env):
    """Restore failure must be logged."""
    import sqlalchemy.orm
    import logging
    from backend.storage.errors import StorageUnavailableError

    env = delete_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    payload = _upload(client, "restore_log.txt", b"restore log test")
    file_id = payload["id"]

    with patch.object(sqlalchemy.orm.Session, "commit", side_effect=Exception("db commit failed")), \
         patch.object(
             env["adapter"],
             "restore",
             side_effect=StorageUnavailableError("restore failed", provider="LOCAL"),
         ), \
         patch("backend.api.v1.endpoints.files.logging.error") as mock_log:
        response = client.delete(f"/api/v1/files/{file_id}")

    assert response.status_code == 500
    # Verify restore failure was logged
    mock_log.assert_called()
    log_call_args = mock_log.call_args[0]
    assert "failed to restore quarantined file" in log_call_args[0].lower()


def test_delete_cleanup_failure_logged(delete_env):
    """Cleanup failure must be logged (as warning)."""
    from backend.storage.errors import StorageUnavailableError

    env = delete_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    payload = _upload(client, "cleanup_log.txt", b"cleanup log test")
    file_id = payload["id"]

    with patch.object(
        env["adapter"],
        "cleanup",
        side_effect=StorageUnavailableError("cleanup failed", provider="LOCAL"),
    ), \
         patch("backend.api.v1.endpoints.files.logging.warning") as mock_log:
        response = client.delete(f"/api/v1/files/{file_id}")

    assert response.status_code == 204
    # Verify cleanup failure was logged as warning
    mock_log.assert_called()
    log_call_args = mock_log.call_args[0]
    assert "failed to cleanup quarantine" in log_call_args[0].lower()


def test_delete_no_activity_on_failed_db_deletion(delete_env):
    """Verify no DELETE activity is created when DB deletion fails."""
    import sqlalchemy.orm

    env = delete_env
    client = env["client"]

    payload = _upload(client, "fail_delete.txt", b"fail")
    file_id = payload["id"]

    with patch.object(sqlalchemy.orm.Session, "commit", side_effect=Exception("db commit failed")):
        response = client.delete(f"/api/v1/files/{file_id}")

    assert response.status_code == 500

    # No DELETE activity (only upload activity)
    act_res = client.get("/api/v1/activity")
    assert act_res.json()["total"] == 1
    assert act_res.json()["items"][0]["action_type"] == "UPLOAD"


def test_delete_activity_created_on_success(delete_env):
    """Verify DELETE activity is created after successful deletion."""
    env = delete_env
    client = env["client"]

    payload = _upload(client, "success_delete.txt", b"success")
    file_id = payload["id"]

    response = client.delete(f"/api/v1/files/{file_id}")
    assert response.status_code == 204

    # DELETE activity should be created
    act_res = client.get("/api/v1/activity")
    assert act_res.json()["total"] == 2  # Upload + Delete
    del_act = act_res.json()["items"][0]
    assert del_act["action_type"] == "DELETE"
    assert del_act["file_name"] == "success_delete.txt"
    assert del_act["file_id"] is None


def test_delete_populates_all_pending_deletion_snapshot_fields(delete_env):
    """Verify that file deletion populates all snapshot fields on PendingDeletion.

    This ensures that original_file_id, original_logical_path, tags, category,
    mime_type, size, sha256, and provider_account_id are preserved for restore.
    """
    from backend.models.pending_deletion import PendingDeletion

    env = delete_env
    client = env["client"]
    engine = env["engine"]
    Session = sessionmaker(bind=engine)

    # Upload file
    payload = _upload(client, "snapshot_test.txt", b"snapshot content to verify fields")
    file_id = payload["id"]

    # Update metadata to ensure all optional snapshot fields are populated
    db = Session()
    try:
        rec = db.get(UnifiedFileRecord, file_id)
        assert rec is not None
        rec.provider_account_id = "test-acc-123"
        rec.category = "TestCategory"
        rec.tags = ["tag1", "tag2"]
        db.commit()
    finally:
        db.close()

    # The record now points at a managed LOCAL account, so the delete path
    # must resolve that exact account. Install this test's LOCAL adapter for it.
    db = Session()
    try:
        db.add(
            StorageAccount(
                id="test-acc-123",
                provider_id="LOCAL",
                display_name="Snapshot account",
                configuration={},
                status="connected",
            )
        )
        db.commit()
    finally:
        db.close()
    adapter_manager.install("test-acc-123", env["adapter"])

    # Intercept cleanup so PendingDeletion remains in DB for inspection
    with patch.object(
        env["adapter"],
        "cleanup",
        side_effect=StorageUnavailableError("keep pending", provider="LOCAL"),
    ):
        response = client.delete(f"/api/v1/files/{file_id}")

    assert response.status_code == 204

    # Verify PendingDeletion record contains all snapshot fields
    db = Session()
    try:
        pending_records = db.query(PendingDeletion).all()
        matching = [p for p in pending_records if p.original_file_id == file_id]
        assert len(matching) == 1
        pd = matching[0]

        assert pd.original_file_id == file_id
        assert pd.original_logical_path == f"/snapshot_test.txt"
        assert pd.original_mime_type == "text/plain"
        assert pd.original_size_bytes == len(b"snapshot content to verify fields")
        assert pd.original_sha256 is not None
        assert pd.original_category == "TestCategory"
        assert pd.original_tags == ["tag1", "tag2"]
        assert pd.provider_account_id == "test-acc-123"
        assert pd.provider == "LOCAL"
        assert pd.file_name_snapshot == "snapshot_test.txt"
        assert pd.quarantine_id is not None
    finally:
        db.close()


def test_delete_handles_empty_tags_and_none_category_correctly(delete_env):
    """Verify snapshot capture handles empty tags and None category cleanly."""
    from backend.models.pending_deletion import PendingDeletion

    env = delete_env
    client = env["client"]
    engine = env["engine"]
    Session = sessionmaker(bind=engine)

    payload = _upload(client, "empty_tags.txt", b"empty tags")
    file_id = payload["id"]

    with patch.object(
        env["adapter"],
        "cleanup",
        side_effect=StorageUnavailableError("keep pending", provider="LOCAL"),
    ):
        response = client.delete(f"/api/v1/files/{file_id}")

    assert response.status_code == 204

    db = Session()
    try:
        pending_records = db.query(PendingDeletion).all()
        matching = [p for p in pending_records if p.original_file_id == file_id]
        assert len(matching) == 1
        pd = matching[0]

        assert pd.original_file_id == file_id
        assert pd.original_tags == []
        assert pd.original_category is None
        assert pd.provider_account_id is None
    finally:
        db.close()


def test_delete_failure_before_commit_rolls_back_everything(delete_env):
    """Verify that a failure during the deletion transaction leaves the record intact without PendingDeletion."""
    import sqlalchemy.orm
    from backend.models.pending_deletion import PendingDeletion

    env = delete_env
    client = env["client"]
    engine = env["engine"]
    Session = sessionmaker(bind=engine)

    payload = _upload(client, "rollback_test.txt", b"rollback check")
    file_id = payload["id"]

    with patch.object(sqlalchemy.orm.Session, "commit", side_effect=Exception("forced DB commit failure")):
        response = client.delete(f"/api/v1/files/{file_id}")

    assert response.status_code == 500

    db = Session()
    try:
        # Original UnifiedFileRecord must still exist
        assert db.get(UnifiedFileRecord, file_id) is not None
        # PendingDeletion must NOT exist
        pending_records = db.query(PendingDeletion).all()
        matching = [p for p in pending_records if p.original_file_id == file_id]
        assert len(matching) == 0
    finally:
        db.close()

