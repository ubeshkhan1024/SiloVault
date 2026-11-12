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
from backend.models.unified_file import UnifiedFileRecord
from backend.storage.local import LocalAdapter
from backend.storage.registry import register, remove
from backend.storage.capabilities import CAPABILITY_MAP
from backend.storage.errors import StorageError, StorageUnavailableError


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

    try:
        yield {
            "client": TestClient(app),
            "engine": engine,
            "uploads_dir": uploads_dir,
        }
    finally:
        # Clean up adapter registration
        remove("LOCAL")
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


def test_delete_cloud_provider_rejected(delete_env):
    """Cloud-provider records must not be deletable from local storage."""
    env = delete_env
    client = env["client"]
    engine = env["engine"]

    # Insert a GOOGLE_DRIVE record directly (no physical file needed).
    session = sessionmaker(bind=engine)()
    try:
        record = UnifiedFileRecord(
            provider="GOOGLE_DRIVE",
            provider_account_id="acc_1",
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
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "LOCAL" in detail


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
    """DB commit fails; adapter.restore() fails.
    The failure must be logged and the original DB exception preserved."""
    import sqlalchemy.orm
    import logging
    from backend.storage.registry import lookup as registry_lookup
    from backend.storage.errors import StorageUnavailableError

    env = delete_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    original_bytes = b"restore fail"
    payload = _upload(client, "restore_fail.txt", original_bytes)
    file_id = payload["id"]
    storage_name = payload["provider_file_id"]

    def mock_lookup(provider):
        adapter, caps = registry_lookup(provider)
        adapter.restore = MagicMock(side_effect=StorageUnavailableError("restore failed", provider="LOCAL"))
        return adapter, caps

    with patch.object(sqlalchemy.orm.Session, "commit", side_effect=Exception("db commit failed")), \
         patch("backend.api.v1.endpoints.files.lookup", side_effect=mock_lookup):
        response = client.delete(f"/api/v1/files/{file_id}")

    assert response.status_code == 500
    assert "restored" in response.json()["detail"].lower()

    # Failed restore leaves a real recoverable quarantine entry
    quarantine_dir = uploads_dir / ".quarantine"
    assert quarantine_dir.exists(), "Quarantine directory should exist when restore fails"
    entries = list(quarantine_dir.iterdir())
    assert len(entries) == 1, "Exactly one quarantine entry should exist when restore fails"
    entry_dir = entries[0]
    assert entry_dir.is_dir(), "Quarantine entry should be a directory"

    # The quarantined file should exist and have correct contents
    quarantined_files = [f for f in entry_dir.iterdir() if f.name != "meta.json"]
    assert len(quarantined_files) == 1, "Exactly one quarantined file should exist"
    quarantined_file = quarantined_files[0]
    assert quarantined_file.is_file(), "Quarantined file should be a regular file"
    assert quarantined_file.read_bytes() == original_bytes, "Quarantined file contents should match original"

    # DB record must still exist
    get_resp = client.get(f"/api/v1/files/{file_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == file_id

    # No DELETE activity
    act_res = client.get("/api/v1/activity")
    assert act_res.json()["total"] == 1  # Only the upload activity


def test_delete_commit_succeeds_cleanup_fails(delete_env):
    """DB commit succeeds; adapter.cleanup() fails.
    HTTP 204 must still be returned and the cleanup failure logged."""
    from backend.storage.registry import lookup as registry_lookup
    from backend.storage.errors import StorageUnavailableError

    env = delete_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    payload = _upload(client, "cleanup_fail.txt", b"cleanup fail")
    file_id = payload["id"]
    storage_name = payload["provider_file_id"]

    def mock_lookup(provider):
        adapter, caps = registry_lookup(provider)
        adapter.cleanup = MagicMock(side_effect=StorageUnavailableError("cleanup failed", provider="LOCAL"))
        return adapter, caps

    with patch("backend.api.v1.endpoints.files.lookup", side_effect=mock_lookup):
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
    from backend.storage.registry import lookup as registry_lookup
    from backend.storage.errors import StorageUnavailableError

    env = delete_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    payload = _upload(client, "restore_log.txt", b"restore log test")
    file_id = payload["id"]

    def mock_lookup(provider):
        adapter, caps = registry_lookup(provider)
        adapter.restore = MagicMock(side_effect=StorageUnavailableError("restore failed", provider="LOCAL"))
        return adapter, caps

    with patch.object(sqlalchemy.orm.Session, "commit", side_effect=Exception("db commit failed")), \
         patch("backend.api.v1.endpoints.files.lookup", side_effect=mock_lookup), \
         patch("backend.api.v1.endpoints.files.logging.error") as mock_log:
        response = client.delete(f"/api/v1/files/{file_id}")

    assert response.status_code == 500
    # Verify restore failure was logged
    mock_log.assert_called()
    log_call_args = mock_log.call_args[0]
    assert "failed to restore quarantined file" in log_call_args[0].lower()


def test_delete_cleanup_failure_logged(delete_env):
    """Cleanup failure must be logged (as warning)."""
    from backend.storage.registry import lookup as registry_lookup
    from backend.storage.errors import StorageUnavailableError

    env = delete_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    payload = _upload(client, "cleanup_log.txt", b"cleanup log test")
    file_id = payload["id"]

    def mock_lookup(provider):
        adapter, caps = registry_lookup(provider)
        adapter.cleanup = MagicMock(side_effect=StorageUnavailableError("cleanup failed", provider="LOCAL"))
        return adapter, caps

    with patch("backend.api.v1.endpoints.files.lookup", side_effect=mock_lookup), \
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
