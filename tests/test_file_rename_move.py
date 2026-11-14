"""Tests for the rename API endpoint ``PATCH /api/v1/files/{file_id}/rename``.

B1.6c Stage 1 — Rename only. The move endpoint is NOT tested here.

Uses isolated temporary upload directories and in-memory SQLite databases so
the development database and real uploads directory are never touched.
Google Drive tests use a fake adapter registered in the storage registry —
no real Drive credentials and no network operations.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import settings
from backend.database import Base, get_db
from backend.main import app
from backend.models.unified_file import UnifiedFileRecord
from backend.storage.capabilities import CAPABILITY_MAP
from backend.storage.errors import (
    StorageAuthenticationError,
    StoragePermissionError,
    StorageUnavailableError,
)
from backend.storage.interfaces import StorageOperations
from backend.storage.local import LocalAdapter
from backend.storage.registry import register, remove
from backend.storage.types import ProviderFileRecord


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rename_env(tmp_path, monkeypatch):
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


class FakeGDriveAdapter(StorageOperations):
    """Fake Google Drive adapter for rename API tests. No network calls."""

    def __init__(
        self,
        rename_side_effect=None,
        rename_return=None,
        move_side_effect=None,
        move_return=None,
    ):
        self.rename_side_effect = rename_side_effect
        self.rename_return = rename_return
        self.rename_calls: list[tuple[str, str]] = []
        self.move_side_effect = move_side_effect
        self.move_return = move_return
        self.move_calls: list[tuple[str, str]] = []

    def upload(self, file_data: bytes, name: str, logical_path: str):
        raise NotImplementedError

    def download(self, provider_file_id: str) -> bytes:
        raise NotImplementedError

    def rename(self, provider_file_id: str, new_name: str) -> ProviderFileRecord:
        self.rename_calls.append((provider_file_id, new_name))
        if self.rename_side_effect is not None:
            raise self.rename_side_effect
        if self.rename_return is not None:
            return self.rename_return
        # Default: return a record that echoes the same provider_file_id
        return ProviderFileRecord(
            provider_file_id=provider_file_id,
            name=new_name,
            logical_path=f"/{provider_file_id}",
            mime_type="application/pdf",
            size_bytes=2048,
        )

    def move(self, source_provider_file_id: str, target_logical_path: str) -> ProviderFileRecord:
        self.move_calls.append((source_provider_file_id, target_logical_path))
        if self.move_side_effect is not None:
            raise self.move_side_effect
        if self.move_return is not None:
            return self.move_return
        # Default: return a record that echoes the same provider_file_id
        return ProviderFileRecord(
            provider_file_id=source_provider_file_id,
            name="original.pdf",
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


@pytest.fixture
def fake_gdrive():
    """Registers a FakeGDriveAdapter under 'GOOGLE_DRIVE' for testing."""
    adapter = FakeGDriveAdapter()
    register("GOOGLE_DRIVE", adapter, CAPABILITY_MAP["GOOGLE_DRIVE"])
    yield adapter
    try:
        remove("GOOGLE_DRIVE")
    except Exception:
        pass


def _upload(client, filename: str, content: bytes) -> dict:
    """Upload a file via the real upload endpoint and return the JSON payload."""
    files = {"file": (filename, content, "application/octet-stream")}
    response = client.post("/api/v1/uploads", files=files)
    assert response.status_code == 200
    return response.json()


def _seed_gdrive_record(engine, provider_file_id: str, name: str) -> UnifiedFileRecord:
    """Insert a GOOGLE_DRIVE UnifiedFileRecord directly into the database."""
    session = sessionmaker(bind=engine)()
    try:
        record = UnifiedFileRecord(
            provider="GOOGLE_DRIVE",
            provider_account_id="acc_1",
            provider_file_id=provider_file_id,
            name=name,
            logical_path=f"/{name}",
            mime_type="application/pdf",
            size_bytes=2048,
            sync_state="COMPLETED",
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record
    finally:
        session.close()


def _seed_local_record(
    engine, provider_file_id: str, name: str, mime_type: str = "text/plain"
) -> UnifiedFileRecord:
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
# LOCAL rename tests
# ---------------------------------------------------------------------------


def test_rename_local_success(rename_env):
    """A successful LOCAL rename updates the DB name and returns 200."""
    env = rename_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    payload = _upload(client, "original.txt", b"rename me")
    file_id = payload["id"]
    storage_name = payload["provider_file_id"]

    response = client.patch(
        f"/api/v1/files/{file_id}/rename", json={"new_name": "renamed.txt"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "renamed.txt"
    # Unrelated metadata is preserved.
    assert body["provider"] == "LOCAL"
    assert body["provider_file_id"] == storage_name
    assert body["logical_path"] == payload["logical_path"]
    assert body["mime_type"] == payload["mime_type"]
    assert body["size_bytes"] == payload["size_bytes"]

    # The database reflects the new name.
    detail = client.get(f"/api/v1/files/{file_id}")
    assert detail.status_code == 200
    assert detail.json()["name"] == "renamed.txt"


def test_rename_local_physical_file_unchanged(rename_env):
    """LOCAL rename must NOT change the physical storage filename.

    The provider_file_id (UUID storage filename) is immutable; only the
    user-facing name in the database changes.
    """
    env = rename_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    payload = _upload(client, "keep.txt", b"physical stays")
    file_id = payload["id"]
    storage_name = payload["provider_file_id"]

    response = client.patch(
        f"/api/v1/files/{file_id}/rename", json={"new_name": "new name.txt"}
    )
    assert response.status_code == 200

    # Physical file is still at the same storage filename with same content.
    physical = uploads_dir / storage_name
    assert physical.exists(), "Physical storage filename must not change"
    assert physical.read_bytes() == b"physical stays"

    # No new physical file was created under the new name.
    assert not (uploads_dir / "new name.txt").exists()
    assert not (uploads_dir / "new_name.txt").exists()

    # provider_file_id in the DB is unchanged.
    detail = client.get(f"/api/v1/files/{file_id}")
    assert detail.json()["provider_file_id"] == storage_name


def test_rename_local_missing_record_returns_404(rename_env):
    """Renaming an unknown internal ID must return HTTP 404."""
    client = rename_env["client"]
    response = client.patch(
        "/api/v1/files/file_does_not_exist_99999/rename", json={"new_name": "x.txt"}
    )
    assert response.status_code == 404
    assert "detail" in response.json()


def test_rename_local_missing_physical_file_returns_404(rename_env):
    """A LOCAL record whose physical file is missing must return 404."""
    env = rename_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_local_record(engine, "ghost_uuid_no_file.txt", "ghost.txt")

    response = client.patch(
        f"/api/v1/files/{record.id}/rename", json={"new_name": "new.txt"}
    )
    assert response.status_code == 404

    # The record must be untouched.
    detail = client.get(f"/api/v1/files/{record.id}")
    assert detail.status_code == 200
    assert detail.json()["name"] == "ghost.txt"


def test_rename_whitespace_only_name_rejected(rename_env):
    """Whitespace-only names must be rejected with 422."""
    env = rename_env
    client = env["client"]

    payload = _upload(client, "ws.txt", b"content")
    file_id = payload["id"]

    for bad_name in ("", "   ", "\t", " \n "):
        response = client.patch(
            f"/api/v1/files/{file_id}/rename", json={"new_name": bad_name}
        )
        assert response.status_code == 422, f"Expected 422 for {bad_name!r}"

    # The name must be unchanged after all failed attempts.
    detail = client.get(f"/api/v1/files/{file_id}")
    assert detail.json()["name"] == "ws.txt"


def test_rename_missing_new_name_field_rejected(rename_env):
    """A request body without new_name must be rejected with 422."""
    env = rename_env
    client = env["client"]

    payload = _upload(client, "nofield.txt", b"content")
    file_id = payload["id"]

    response = client.patch(f"/api/v1/files/{file_id}/rename", json={})
    assert response.status_code == 422


def test_rename_name_too_long_rejected(rename_env):
    """A name exceeding 255 characters must be rejected with 422."""
    env = rename_env
    client = env["client"]

    payload = _upload(client, "long.txt", b"content")
    file_id = payload["id"]

    response = client.patch(
        f"/api/v1/files/{file_id}/rename", json={"new_name": "a" * 256 + ".txt"}
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Provider dispatch and error mapping
# ---------------------------------------------------------------------------


def test_rename_unavailable_provider_returns_503(rename_env):
    """A GOOGLE_DRIVE record with no registered adapter must return 503."""
    env = rename_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_gdrive_record(engine, "gdrive_abc123", "cloud.pdf")

    # No GOOGLE_DRIVE adapter is registered in this fixture.
    response = client.patch(
        f"/api/v1/files/{record.id}/rename", json={"new_name": "new.pdf"}
    )
    assert response.status_code == 503
    assert "detail" in response.json()

    # The record must be untouched.
    detail = client.get(f"/api/v1/files/{record.id}")
    assert detail.json()["name"] == "cloud.pdf"


def test_rename_authentication_error_returns_401(rename_env, fake_gdrive):
    """StorageAuthenticationError from the adapter must map to HTTP 401."""
    env = rename_env
    client = env["client"]
    engine = env["engine"]

    fake_gdrive.rename_side_effect = StorageAuthenticationError(
        detail="Token revoked", provider="GOOGLE_DRIVE"
    )

    record = _seed_gdrive_record(engine, "gdrive_authfail", "auth.pdf")

    response = client.patch(
        f"/api/v1/files/{record.id}/rename", json={"new_name": "new.pdf"}
    )
    assert response.status_code == 401

    # The record must be untouched.
    detail = client.get(f"/api/v1/files/{record.id}")
    assert detail.json()["name"] == "auth.pdf"


def test_rename_permission_error_returns_403(rename_env, fake_gdrive):
    """StoragePermissionError from the adapter must map to HTTP 403."""
    env = rename_env
    client = env["client"]
    engine = env["engine"]

    fake_gdrive.rename_side_effect = StoragePermissionError(
        detail="Access denied", provider="GOOGLE_DRIVE"
    )

    record = _seed_gdrive_record(engine, "gdrive_permfail", "perm.pdf")

    response = client.patch(
        f"/api/v1/files/{record.id}/rename", json={"new_name": "new.pdf"}
    )
    assert response.status_code == 403

    # The record must be untouched.
    detail = client.get(f"/api/v1/files/{record.id}")
    assert detail.json()["name"] == "perm.pdf"


def test_rename_unavailable_error_returns_503(rename_env, fake_gdrive):
    """StorageUnavailableError from the adapter must map to HTTP 503."""
    env = rename_env
    client = env["client"]
    engine = env["engine"]

    fake_gdrive.rename_side_effect = StorageUnavailableError(
        detail="Drive API down", provider="GOOGLE_DRIVE"
    )

    record = _seed_gdrive_record(engine, "gdrive_unavail", "unavail.pdf")

    response = client.patch(
        f"/api/v1/files/{record.id}/rename", json={"new_name": "new.pdf"}
    )
    assert response.status_code == 503

    # The record must be untouched.
    detail = client.get(f"/api/v1/files/{record.id}")
    assert detail.json()["name"] == "unavail.pdf"


def test_rename_provider_id_mismatch_rejected(rename_env, fake_gdrive):
    """A provider record with a mismatched provider_file_id must be rejected
    with 500 and the database must remain unchanged."""
    env = rename_env
    client = env["client"]
    engine = env["engine"]

    fake_gdrive.rename_return = ProviderFileRecord(
        provider_file_id="gdrive_some_other_file",
        name="new.pdf",
        logical_path="/gdrive_some_other_file",
        mime_type="application/pdf",
        size_bytes=2048,
    )

    record = _seed_gdrive_record(engine, "gdrive_expected", "expected.pdf")

    response = client.patch(
        f"/api/v1/files/{record.id}/rename", json={"new_name": "new.pdf"}
    )
    assert response.status_code == 500
    assert "mismatch" in response.json()["detail"].lower()

    # The database record must be completely untouched.
    detail = client.get(f"/api/v1/files/{record.id}")
    assert detail.json()["name"] == "expected.pdf"
    assert detail.json()["provider_file_id"] == "gdrive_expected"


# ---------------------------------------------------------------------------
# GOOGLE_DRIVE rename success (mocked adapter only)
# ---------------------------------------------------------------------------


def test_rename_gdrive_success_mocked(rename_env, fake_gdrive):
    """A successful GOOGLE_DRIVE rename persists the new name in the DB."""
    env = rename_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_gdrive_record(engine, "gdrive_abc123", "original.pdf")

    response = client.patch(
        f"/api/v1/files/{record.id}/rename", json={"new_name": "renamed.pdf"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "renamed.pdf"
    assert body["provider"] == "GOOGLE_DRIVE"
    assert body["provider_file_id"] == "gdrive_abc123"
    # Unrelated metadata is preserved.
    assert body["logical_path"] == "/original.pdf"
    assert body["mime_type"] == "application/pdf"
    assert body["size_bytes"] == 2048

    # The adapter was called with the correct arguments.
    assert fake_gdrive.rename_calls == [("gdrive_abc123", "renamed.pdf")]

    # The database reflects the new name.
    detail = client.get(f"/api/v1/files/{record.id}")
    assert detail.json()["name"] == "renamed.pdf"


# ---------------------------------------------------------------------------
# Activity logging
# ---------------------------------------------------------------------------


def test_rename_activity_logged_with_old_and_new_name(rename_env):
    """A successful rename must create a RENAME activity entry carrying the
    old name as file_name and the new name in details."""
    env = rename_env
    client = env["client"]

    payload = _upload(client, "activity.txt", b"content")
    file_id = payload["id"]

    response = client.patch(
        f"/api/v1/files/{file_id}/rename", json={"new_name": "activity_new.txt"}
    )
    assert response.status_code == 200

    act_res = client.get("/api/v1/activity")
    assert act_res.status_code == 200
    items = act_res.json()["items"]
    rename_items = [i for i in items if i["action_type"] == "RENAME"]
    assert len(rename_items) == 1
    entry = rename_items[0]
    assert entry["file_name"] == "activity.txt"  # old name snapshot
    assert entry["provider"] == "LOCAL"
    assert entry["file_id"] == file_id
    assert entry["details"]["new_name"] == "activity_new.txt"
    assert entry["details"]["provider_file_id"] == payload["provider_file_id"]


def test_rename_failed_does_not_log_activity(rename_env, fake_gdrive):
    """A failed rename (permission error) must NOT create a RENAME activity."""
    env = rename_env
    client = env["client"]
    engine = env["engine"]

    fake_gdrive.rename_side_effect = StoragePermissionError(
        detail="Access denied", provider="GOOGLE_DRIVE"
    )

    record = _seed_gdrive_record(engine, "gdrive_noact", "noact.pdf")

    response = client.patch(
        f"/api/v1/files/{record.id}/rename", json={"new_name": "new.pdf"}
    )
    assert response.status_code == 403

    act_res = client.get("/api/v1/activity")
    items = act_res.json()["items"]
    assert all(i["action_type"] != "RENAME" for i in items)


# ---------------------------------------------------------------------------
# Regression — existing endpoints must remain functional
# ---------------------------------------------------------------------------


def test_existing_endpoints_unchanged_after_rename(rename_env):
    """Upload, list, detail, and download must behave exactly as before
    after the rename endpoint has been added and used."""
    env = rename_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    # Upload still works
    payload = _upload(client, "regression.txt", b"still works")
    file_id = payload["id"]
    storage_name = payload["provider_file_id"]

    # List still works
    list_resp = client.get("/api/v1/files")
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1

    # Detail still works
    detail_resp = client.get(f"/api/v1/files/{file_id}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["name"] == "regression.txt"

    # Download still works
    dl_resp = client.get(f"/api/v1/files/{file_id}/download")
    assert dl_resp.status_code == 200
    assert dl_resp.content == b"still works"

    # Rename works
    rename_resp = client.patch(
        f"/api/v1/files/{file_id}/rename", json={"new_name": "renamed.txt"}
    )
    assert rename_resp.status_code == 200
    assert rename_resp.json()["name"] == "renamed.txt"

    # After rename, list/detail/download still work
    list_resp = client.get("/api/v1/files")
    assert list_resp.json()["total"] == 1
    assert list_resp.json()["items"][0]["name"] == "renamed.txt"

    detail_resp = client.get(f"/api/v1/files/{file_id}")
    assert detail_resp.json()["name"] == "renamed.txt"

    dl_resp = client.get(f"/api/v1/files/{file_id}/download")
    assert dl_resp.status_code == 200
    assert dl_resp.content == b"still works"

    # Physical file untouched
    assert (uploads_dir / storage_name).exists()


# ---------------------------------------------------------------------------
# LOCAL move tests
# ---------------------------------------------------------------------------


def test_move_local_success(rename_env):
    """A successful LOCAL logical move updates the DB logical_path and
    returns 200. The name and provider_file_id must be preserved."""
    env = rename_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    payload = _upload(client, "original.txt", b"move me")
    file_id = payload["id"]
    storage_name = payload["provider_file_id"]

    response = client.patch(
        f"/api/v1/files/{file_id}/move",
        json={"target_logical_path": "/New/Folder/moved.txt"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["logical_path"] == "/New/Folder/moved.txt"
    # Unrelated metadata is preserved.
    assert body["name"] == "original.txt"
    assert body["provider"] == "LOCAL"
    assert body["provider_file_id"] == storage_name
    assert body["mime_type"] == payload["mime_type"]
    assert body["size_bytes"] == payload["size_bytes"]

    # The database reflects the new path.
    detail = client.get(f"/api/v1/files/{file_id}")
    assert detail.status_code == 200
    assert detail.json()["logical_path"] == "/New/Folder/moved.txt"


def test_move_local_physical_file_unchanged(rename_env):
    """LOCAL move must NOT change the physical storage location.

    The provider_file_id (UUID storage filename) is immutable; only the
    logical_path in the database changes.
    """
    env = rename_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    payload = _upload(client, "keep.txt", b"physical stays")
    file_id = payload["id"]
    storage_name = payload["provider_file_id"]

    response = client.patch(
        f"/api/v1/files/{file_id}/move",
        json={"target_logical_path": "/New/Folder/moved.txt"},
    )
    assert response.status_code == 200

    # Physical file is still at the same storage filename with same content.
    physical = uploads_dir / storage_name
    assert physical.exists(), "Physical storage filename must not change"
    assert physical.read_bytes() == b"physical stays"

    # No new physical file was created.
    assert len(list(uploads_dir.iterdir())) == 1

    # provider_file_id in the DB is unchanged.
    detail = client.get(f"/api/v1/files/{file_id}")
    assert detail.json()["provider_file_id"] == storage_name


def test_move_local_missing_record_returns_404(rename_env):
    """Moving an unknown internal ID must return HTTP 404."""
    client = rename_env["client"]
    response = client.patch(
        "/api/v1/files/file_does_not_exist_99999/move",
        json={"target_logical_path": "/new/path.txt"},
    )
    assert response.status_code == 404
    assert "detail" in response.json()


def test_move_local_missing_physical_file_returns_404(rename_env):
    """A LOCAL record whose physical file is missing must return 404."""
    env = rename_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_local_record(engine, "ghost_uuid_no_file.txt", "ghost.txt")

    response = client.patch(
        f"/api/v1/files/{record.id}/move",
        json={"target_logical_path": "/new/path.txt"},
    )
    assert response.status_code == 404

    # The record must be untouched.
    detail = client.get(f"/api/v1/files/{record.id}")
    assert detail.status_code == 200
    assert detail.json()["logical_path"] == "/ghost.txt"


def test_move_whitespace_only_path_rejected(rename_env):
    """Whitespace-only paths must be rejected with 422."""
    env = rename_env
    client = env["client"]

    payload = _upload(client, "ws.txt", b"content")
    file_id = payload["id"]

    for bad_path in ("", "   ", "\t", " \n "):
        response = client.patch(
            f"/api/v1/files/{file_id}/move",
            json={"target_logical_path": bad_path},
        )
        assert response.status_code == 422, f"Expected 422 for {bad_path!r}"

    # The path must be unchanged after all failed attempts.
    detail = client.get(f"/api/v1/files/{file_id}")
    assert detail.json()["logical_path"] == "/ws.txt"


def test_move_missing_target_field_rejected(rename_env):
    """A request body without target_logical_path must be rejected with 422."""
    env = rename_env
    client = env["client"]

    payload = _upload(client, "nofield.txt", b"content")
    file_id = payload["id"]

    response = client.patch(f"/api/v1/files/{file_id}/move", json={})
    assert response.status_code == 422


def test_move_path_too_long_rejected(rename_env):
    """A path exceeding 1024 characters must be rejected with 422."""
    env = rename_env
    client = env["client"]

    payload = _upload(client, "long.txt", b"content")
    file_id = payload["id"]

    response = client.patch(
        f"/api/v1/files/{file_id}/move",
        json={"target_logical_path": "/" + "a" * 1024},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Move provider dispatch and error mapping
# ---------------------------------------------------------------------------


def test_move_unavailable_provider_returns_503(rename_env):
    """A GOOGLE_DRIVE record with no registered adapter must return 503."""
    env = rename_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_gdrive_record(engine, "gdrive_abc123", "cloud.pdf")

    # No GOOGLE_DRIVE adapter is registered in this fixture.
    response = client.patch(
        f"/api/v1/files/{record.id}/move",
        json={"target_logical_path": "/new/path.pdf"},
    )
    assert response.status_code == 503
    assert "detail" in response.json()

    # The record must be untouched.
    detail = client.get(f"/api/v1/files/{record.id}")
    assert detail.json()["logical_path"] == "/cloud.pdf"


def test_move_authentication_error_returns_401(rename_env, fake_gdrive):
    """StorageAuthenticationError from the adapter must map to HTTP 401."""
    env = rename_env
    client = env["client"]
    engine = env["engine"]

    fake_gdrive.move_side_effect = StorageAuthenticationError(
        detail="Token revoked", provider="GOOGLE_DRIVE"
    )

    record = _seed_gdrive_record(engine, "gdrive_authfail", "auth.pdf")

    response = client.patch(
        f"/api/v1/files/{record.id}/move",
        json={"target_logical_path": "/new/path.pdf"},
    )
    assert response.status_code == 401

    # The record must be untouched.
    detail = client.get(f"/api/v1/files/{record.id}")
    assert detail.json()["logical_path"] == "/auth.pdf"


def test_move_permission_error_returns_403(rename_env, fake_gdrive):
    """StoragePermissionError from the adapter must map to HTTP 403."""
    env = rename_env
    client = env["client"]
    engine = env["engine"]

    fake_gdrive.move_side_effect = StoragePermissionError(
        detail="Access denied", provider="GOOGLE_DRIVE"
    )

    record = _seed_gdrive_record(engine, "gdrive_permfail", "perm.pdf")

    response = client.patch(
        f"/api/v1/files/{record.id}/move",
        json={"target_logical_path": "/new/path.pdf"},
    )
    assert response.status_code == 403

    # The record must be untouched.
    detail = client.get(f"/api/v1/files/{record.id}")
    assert detail.json()["logical_path"] == "/perm.pdf"


def test_move_unavailable_error_returns_503(rename_env, fake_gdrive):
    """StorageUnavailableError from the adapter must map to HTTP 503."""
    env = rename_env
    client = env["client"]
    engine = env["engine"]

    fake_gdrive.move_side_effect = StorageUnavailableError(
        detail="Drive API down", provider="GOOGLE_DRIVE"
    )

    record = _seed_gdrive_record(engine, "gdrive_unavail", "unavail.pdf")

    response = client.patch(
        f"/api/v1/files/{record.id}/move",
        json={"target_logical_path": "/new/path.pdf"},
    )
    assert response.status_code == 503

    # The record must be untouched.
    detail = client.get(f"/api/v1/files/{record.id}")
    assert detail.json()["logical_path"] == "/unavail.pdf"


def test_move_provider_id_mismatch_rejected(rename_env, fake_gdrive):
    """A provider record with a mismatched provider_file_id must be rejected
    with 500 and the database must remain unchanged."""
    env = rename_env
    client = env["client"]
    engine = env["engine"]

    fake_gdrive.move_return = ProviderFileRecord(
        provider_file_id="gdrive_some_other_file",
        name="new.pdf",
        logical_path="/new/path.pdf",
        mime_type="application/pdf",
        size_bytes=2048,
    )

    record = _seed_gdrive_record(engine, "gdrive_expected", "expected.pdf")

    response = client.patch(
        f"/api/v1/files/{record.id}/move",
        json={"target_logical_path": "/new/path.pdf"},
    )
    assert response.status_code == 500
    assert "mismatch" in response.json()["detail"].lower()

    # The database record must be completely untouched.
    detail = client.get(f"/api/v1/files/{record.id}")
    assert detail.json()["logical_path"] == "/expected.pdf"
    assert detail.json()["provider_file_id"] == "gdrive_expected"


# ---------------------------------------------------------------------------
# GOOGLE_DRIVE move success (mocked adapter only)
# ---------------------------------------------------------------------------


def test_move_gdrive_success_mocked(rename_env, fake_gdrive):
    """A successful GOOGLE_DRIVE logical move persists the new path in the
    DB. The name and provider_file_id must be preserved."""
    env = rename_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_gdrive_record(engine, "gdrive_abc123", "original.pdf")
    target_path = "/Archive/2024/moved.pdf"

    response = client.patch(
        f"/api/v1/files/{record.id}/move",
        json={"target_logical_path": target_path},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["logical_path"] == target_path
    assert body["provider"] == "GOOGLE_DRIVE"
    assert body["provider_file_id"] == "gdrive_abc123"
    # Unrelated metadata is preserved.
    assert body["name"] == "original.pdf"
    assert body["mime_type"] == "application/pdf"
    assert body["size_bytes"] == 2048

    # The adapter was called with the correct arguments.
    assert fake_gdrive.move_calls == [("gdrive_abc123", target_path)]

    # The database reflects the new path.
    detail = client.get(f"/api/v1/files/{record.id}")
    assert detail.json()["logical_path"] == target_path


# ---------------------------------------------------------------------------
# Move activity logging
# ---------------------------------------------------------------------------


def test_move_activity_logged_with_old_and_new_path(rename_env):
    """A successful move must create a MOVE activity entry carrying the
    old path and the new path in details."""
    env = rename_env
    client = env["client"]

    payload = _upload(client, "activity.txt", b"content")
    file_id = payload["id"]

    response = client.patch(
        f"/api/v1/files/{file_id}/move",
        json={"target_logical_path": "/New/Folder/activity.txt"},
    )
    assert response.status_code == 200

    act_res = client.get("/api/v1/activity")
    assert act_res.status_code == 200
    items = act_res.json()["items"]
    move_items = [i for i in items if i["action_type"] == "MOVE"]
    assert len(move_items) == 1
    entry = move_items[0]
    assert entry["file_name"] == "activity.txt"
    assert entry["provider"] == "LOCAL"
    assert entry["file_id"] == file_id
    assert entry["details"]["old_logical_path"] == payload["logical_path"]
    assert entry["details"]["new_logical_path"] == "/New/Folder/activity.txt"
    assert entry["details"]["provider_file_id"] == payload["provider_file_id"]


def test_move_failed_does_not_log_activity(rename_env, fake_gdrive):
    """A failed move (permission error) must NOT create a MOVE activity."""
    env = rename_env
    client = env["client"]
    engine = env["engine"]

    fake_gdrive.move_side_effect = StoragePermissionError(
        detail="Access denied", provider="GOOGLE_DRIVE"
    )

    record = _seed_gdrive_record(engine, "gdrive_noact", "noact.pdf")

    response = client.patch(
        f"/api/v1/files/{record.id}/move",
        json={"target_logical_path": "/new/path.pdf"},
    )
    assert response.status_code == 403

    act_res = client.get("/api/v1/activity")
    items = act_res.json()["items"]
    assert all(i["action_type"] != "MOVE" for i in items)


# ---------------------------------------------------------------------------
# Regression — existing endpoints must remain functional after move
# ---------------------------------------------------------------------------


def test_existing_endpoints_unchanged_after_move(rename_env):
    """Upload, list, detail, download, and rename must behave exactly as
    before after the move endpoint has been added and used."""
    env = rename_env
    client = env["client"]
    uploads_dir = env["uploads_dir"]

    # Upload still works
    payload = _upload(client, "regression.txt", b"still works")
    file_id = payload["id"]
    storage_name = payload["provider_file_id"]

    # List still works
    list_resp = client.get("/api/v1/files")
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1

    # Detail still works
    detail_resp = client.get(f"/api/v1/files/{file_id}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["name"] == "regression.txt"

    # Download still works
    dl_resp = client.get(f"/api/v1/files/{file_id}/download")
    assert dl_resp.status_code == 200
    assert dl_resp.content == b"still works"

    # Rename still works
    rename_resp = client.patch(
        f"/api/v1/files/{file_id}/rename", json={"new_name": "renamed.txt"}
    )
    assert rename_resp.status_code == 200
    assert rename_resp.json()["name"] == "renamed.txt"

    # Move works
    move_resp = client.patch(
        f"/api/v1/files/{file_id}/move",
        json={"target_logical_path": "/New/Folder/renamed.txt"},
    )
    assert move_resp.status_code == 200
    assert move_resp.json()["logical_path"] == "/New/Folder/renamed.txt"
    assert move_resp.json()["name"] == "renamed.txt"

    # After move, list/detail/download still work
    list_resp = client.get("/api/v1/files")
    assert list_resp.json()["total"] == 1
    assert list_resp.json()["items"][0]["logical_path"] == "/New/Folder/renamed.txt"

    detail_resp = client.get(f"/api/v1/files/{file_id}")
    assert detail_resp.json()["logical_path"] == "/New/Folder/renamed.txt"

    dl_resp = client.get(f"/api/v1/files/{file_id}/download")
    assert dl_resp.status_code == 200
    assert dl_resp.content == b"still works"

    # Physical file untouched
    assert (uploads_dir / storage_name).exists()
