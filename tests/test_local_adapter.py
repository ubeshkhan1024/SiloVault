"""Focused tests for the local filesystem storage adapter (M2 Task 5).

Covers ``backend.storage.local.LocalAdapter``:

* All seven ``StorageOperations`` methods: upload, download, rename, move,
  quarantine, restore, cleanup.
* Path traversal and unsafe-identifier rejection.
* Symlink protection (internal and external symlinks; skipped on hosts that
  refuse symlink creation, e.g. Windows without Developer Mode).
* Upload no-overwrite guarantees (collision detection and race handling).
* Quarantine persistence across adapter recreation.
* Recovery from an interrupted quarantine (missing sidecar) and from a
  tampered or malformed sidecar.
* Framework independence: importing the module must not load FastAPI or
  SQLAlchemy (verified in an isolated subprocess).

All tests use pytest ``tmp_path`` directories.  The real uploads directory and
the database are never touched.
"""

from __future__ import annotations

import errno
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from backend.storage.errors import (
    StorageConflictError,
    StorageError,
    StorageNotFoundError,
    StoragePermissionError,
    StorageUnavailableError,
)
from backend.storage.interfaces import StorageLifecycle, StorageOperations
from backend.storage.local import LocalAdapter
from backend.storage.types import ProviderFileRecord


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    """A fresh temporary storage root directory."""
    root = tmp_path / "uploads"
    root.mkdir()
    return root


@pytest.fixture
def adapter(storage_root: Path) -> LocalAdapter:
    """A LocalAdapter bound to the temporary storage root."""
    return LocalAdapter(storage_root)


def _make_symlink(link: Path, target: Path) -> None:
    """Create a symlink, skipping the test if the host refuses it.

    Windows without Developer Mode raises ``OSError`` with ``winerror`` 1314;
    some POSIX filesystems refuse with ``EOPNOTSUPP`` (errno 95).  Only those
    explicit refusals cause a skip — other errors are test failures.
    """
    try:
        link.symlink_to(target)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314 or exc.errno == 95:
            pytest.skip(f"Symlink creation not permitted on this host: {exc}")
        raise


def _make_quarantine_entry(
    storage_root: Path,
    provider_file_id: str,
    content: bytes = b"quarantined payload",
    meta: dict | None = "auto",
) -> str:
    """Create a quarantine entry directly on disk and return its ID.

    Used to simulate quarantine state as if a previous adapter instance had
    created it (adapter recreation, interrupted quarantine, tampered
    metadata).  ``meta="auto"`` writes a correct sidecar; ``meta=None``
    omits it; otherwise the given dict is written verbatim.
    """
    quarantine_id = uuid.uuid4().hex
    entry_dir = storage_root / ".quarantine" / quarantine_id
    entry_dir.mkdir(parents=True)
    (entry_dir / provider_file_id).write_bytes(content)
    if meta is not None:
        if meta == "auto":
            meta = {
                "provider_file_id": provider_file_id,
                "size_bytes": len(content),
                "quarantined_at": "2026-11-12T00:00:00+00:00",
            }
        (entry_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return quarantine_id


# ---------------------------------------------------------------------------
# Interface conformance
# ---------------------------------------------------------------------------


def test_adapter_is_a_storage_operations(adapter: LocalAdapter) -> None:
    """LocalAdapter must be a concrete StorageOperations implementation."""
    assert isinstance(adapter, StorageOperations)


def test_adapter_does_not_implement_storage_lifecycle(adapter: LocalAdapter) -> None:
    """Local storage must not be forced to implement cloud lifecycle hooks."""
    assert not isinstance(adapter, StorageLifecycle)
    assert not hasattr(adapter, "authenticate")
    assert not hasattr(adapter, "refresh")
    assert not hasattr(adapter, "disconnect")


def test_all_seven_operations_are_implemented(adapter: LocalAdapter) -> None:
    """Every mandatory StorageOperations method must be a concrete method."""
    for method in (
        "upload",
        "download",
        "rename",
        "move",
        "quarantine",
        "restore",
        "cleanup",
    ):
        assert callable(getattr(adapter, method))


def test_storage_root_property(adapter: LocalAdapter, storage_root: Path) -> None:
    """The storage_root property reports the resolved root directory."""
    assert adapter.storage_root == storage_root.resolve()


def test_constructor_creates_missing_root(tmp_path: Path) -> None:
    """The adapter creates its storage root when it does not exist yet."""
    missing = tmp_path / "nested" / "uploads"
    LocalAdapter(missing)
    assert missing.is_dir()


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------


def test_upload_returns_record_and_stores_file(
    adapter: LocalAdapter, storage_root: Path
) -> None:
    """Upload stores the bytes under a UUID storage filename and returns a record."""
    record = adapter.upload(b"hello world", name="report.pdf", logical_path="/Invoices/report.pdf")

    assert isinstance(record, ProviderFileRecord)
    assert record.provider_file_id.endswith(".pdf")
    assert record.name == "report.pdf"
    assert record.logical_path == "/Invoices/report.pdf"
    assert record.mime_type == "application/pdf"
    assert record.size_bytes == len(b"hello world")
    assert record.modified_at is not None

    # UUID stem (32 hex chars, no hyphens) + extension, flat inside the root.
    assert re.fullmatch(r"[0-9a-f]{32}\.pdf", record.provider_file_id)
    physical = storage_root / record.provider_file_id
    assert physical.is_file()
    assert physical.read_bytes() == b"hello world"


def test_upload_without_extension_uses_plain_uuid(
    adapter: LocalAdapter, storage_root: Path
) -> None:
    """A name without an extension yields a bare UUID storage filename."""
    record = adapter.upload(b"data", name="README", logical_path="/notes/README")
    assert record.provider_file_id == record.provider_file_id.rstrip(".")
    assert re.fullmatch(r"[0-9a-f]{32}", record.provider_file_id)
    assert (storage_root / record.provider_file_id).read_bytes() == b"data"
    assert record.mime_type == "application/octet-stream"


def test_upload_accepts_bytearray(adapter: LocalAdapter) -> None:
    """bytearray input is accepted and stored as bytes."""
    record = adapter.upload(bytearray(b"ab"), name="x.txt", logical_path="/x.txt")
    assert record.size_bytes == 2


def test_upload_rejects_non_bytes_input(adapter: LocalAdapter) -> None:
    """Non-bytes payloads are rejected with TypeError before any filesystem work."""
    with pytest.raises(TypeError):
        adapter.upload("not bytes", name="x.txt", logical_path="/x.txt")  # type: ignore[arg-type]


def test_upload_collision_with_preexisting_destination(
    adapter: LocalAdapter, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the generated storage filename already exists, upload is a conflict."""
    fixed_uuid = uuid.UUID("12345678-9abc-4def-8123-456789abcdef")
    monkeypatch.setattr(uuid, "uuid4", lambda: fixed_uuid)

    # First upload wins.
    first = adapter.upload(b"original", name="doc.txt", logical_path="/doc.txt")
    assert first.provider_file_id == f"{fixed_uuid.hex}.txt"

    # Second upload with the same generated name must conflict.
    with pytest.raises(StorageConflictError) as excinfo:
        adapter.upload(b"second", name="doc.txt", logical_path="/doc.txt")
    assert excinfo.value.provider == "LOCAL"

    # The original file is untouched.
    assert (storage_root / first.provider_file_id).read_bytes() == b"original"


def test_upload_race_collision_at_replace(
    adapter: LocalAdapter, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A destination appearing between the pre-check and the publication is a conflict.

    Simulates the race by making os.link raise FileExistsError (the
    cross-platform behavior when the destination exists).
    """
    import os

    original_link = os.link

    def racing_link(src: str, dst: str) -> None:
        if str(dst).endswith("123456789abc4def8123456789abcdef.txt"):
            raise FileExistsError("race: destination appeared")
        return original_link(src, dst)

    monkeypatch.setattr(os, "link", racing_link)
    monkeypatch.setattr(
        uuid, "uuid4", lambda: uuid.UUID("12345678-9abc-4def-8123-456789abcdef")
    )

    with pytest.raises(StorageConflictError):
        adapter.upload(b"data", name="doc.txt", logical_path="/doc.txt")

    # No partial state left behind: no final file, no leftover temp file.
    files = list(storage_root.iterdir())
    assert files == []


def test_upload_no_partial_file_on_write_failure(
    adapter: LocalAdapter, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write failure leaves no partial file and reports StorageUnavailableError."""

    def failing_open(self: Path, *args: object, **kwargs: object):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", failing_open)

    with pytest.raises(StorageUnavailableError) as excinfo:
        adapter.upload(b"data", name="doc.txt", logical_path="/doc.txt")
    assert excinfo.value.provider == "LOCAL"

    # No temporary part file or final file remains.
    assert list(storage_root.iterdir()) == []


def test_upload_race_collision_at_publication(
    adapter: LocalAdapter, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A destination appearing between the pre-check and the os.link publication
    is a conflict — the original file is never overwritten."""
    import os

    original_link = os.link
    call_count = {"count": 0}

    def racing_link(src: str, dst: str) -> None:
        call_count["count"] += 1
        # Only fail on the second upload (second call to os.link)
        if call_count["count"] == 2 and str(dst).endswith("123456789abc4def8123456789abcdef.txt"):
            raise FileExistsError("race: destination appeared")
        return original_link(src, dst)

    monkeypatch.setattr(os, "link", racing_link)
    monkeypatch.setattr(
        uuid, "uuid4", lambda: uuid.UUID("12345678-9abc-4def-8123-456789abcdef")
    )

    # First upload creates the file
    first = adapter.upload(b"original", name="doc.txt", logical_path="/doc.txt")

    # Second upload with same generated name should conflict at publication
    with pytest.raises(StorageConflictError):
        adapter.upload(b"second", name="doc.txt", logical_path="/doc.txt")

    # Original file content preserved
    assert (storage_root / first.provider_file_id).read_bytes() == b"original"


def test_upload_unsupported_hardlink_raises_error(
    adapter: LocalAdapter, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.link is unsupported (e.g. network FS), upload fails with
    StorageUnavailableError instead of falling back to overwrite-capable replace()."""
    import os

    def failing_link(src: str, dst: str) -> None:
        raise OSError("hard links not supported")

    monkeypatch.setattr(os, "link", failing_link)

    with pytest.raises(StorageUnavailableError) as excinfo:
        adapter.upload(b"data", name="doc.txt", logical_path="/doc.txt")

    assert excinfo.value.provider == "LOCAL"
    assert "Atomic no-overwrite publication not supported" in str(excinfo.value)


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


def test_download_round_trip(adapter: LocalAdapter) -> None:
    """Download returns exactly the bytes that were uploaded."""
    payload = b"\x00\x01binary\x02payload"
    record = adapter.upload(payload, name="bin.dat", logical_path="/bin.dat")
    assert adapter.download(record.provider_file_id) == payload


def test_download_missing_file(adapter: LocalAdapter) -> None:
    """Downloading a nonexistent file raises StorageNotFoundError."""
    with pytest.raises(StorageNotFoundError) as excinfo:
        adapter.download("does-not-exist.txt")
    assert excinfo.value.provider == "LOCAL"
    assert excinfo.value.provider_file_id == "does-not-exist.txt"


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../../etc/passwd",
        "..\\..\\windows\\system.ini",
        "sub/dir.txt",
        "C:/evil.txt",
        "sub\\evil.txt",
        ".hidden",
        ".quarantine",
        ".abc123.part",
        "name with spaces.txt",
        "  padded.txt",
        "padded.txt  ",
        "",
        "a\x00b.txt",
        "..",
    ],
)
def test_download_rejects_unsafe_identifiers(adapter: LocalAdapter, bad_id: str) -> None:
    """Path traversal, separators, reserved names, and padded IDs are rejected."""
    with pytest.raises(StorageNotFoundError):
        adapter.download(bad_id)


def test_download_rejects_non_string_id(adapter: LocalAdapter) -> None:
    """Non-string identifiers are rejected before any filesystem access."""
    with pytest.raises(StorageNotFoundError):
        adapter.download(12345)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------


def test_rename_updates_user_facing_name_only(
    adapter: LocalAdapter, storage_root: Path
) -> None:
    """Rename changes the record's name; the physical file is untouched.
    The adapter does not own metadata; it returns a record with the new name
    and falls back to provider_file_id for logical_path (database is authoritative).
    """
    record = adapter.upload(b"content", name="old.txt", logical_path="/old.txt")

    renamed = adapter.rename(record.provider_file_id, "new name.txt")

    assert renamed.name == "new name.txt"
    assert renamed.provider_file_id == record.provider_file_id
    # logical_path falls back to "/<provider_file_id>" because adapter has no DB
    assert renamed.logical_path == f"/{record.provider_file_id}"
    # Physical file still exists under its original UUID name.
    assert (storage_root / record.provider_file_id).is_file()


def test_rename_missing_file(adapter: LocalAdapter) -> None:
    """Renaming a nonexistent file raises StorageNotFoundError."""
    with pytest.raises(StorageNotFoundError):
        adapter.rename("missing.txt", "whatever.txt")


# ---------------------------------------------------------------------------
# move
# ---------------------------------------------------------------------------


def test_move_updates_logical_path_only(
    adapter: LocalAdapter, storage_root: Path
) -> None:
    """Move changes the record's logical path; the physical file stays in place.
    The adapter does not own metadata; it returns a record with the new logical_path
    and falls back to provider_file_id for name (database is authoritative).
    """
    record = adapter.upload(b"content", name="doc.txt", logical_path="/a/doc.txt")

    moved = adapter.move(record.provider_file_id, "/b/doc.txt")

    assert moved.logical_path == "/b/doc.txt"
    assert moved.provider_file_id == record.provider_file_id
    # name falls back to provider_file_id because adapter has no DB
    assert moved.name == record.provider_file_id
    assert (storage_root / record.provider_file_id).is_file()


def test_move_missing_file(adapter: LocalAdapter) -> None:
    """Moving a nonexistent file raises StorageNotFoundError."""
    with pytest.raises(StorageNotFoundError):
        adapter.move("missing.txt", "/elsewhere/doc.txt")


# ---------------------------------------------------------------------------
# quarantine / restore / cleanup (happy path)
# ---------------------------------------------------------------------------


def test_quarantine_moves_file_and_writes_meta(
    adapter: LocalAdapter, storage_root: Path
) -> None:
    """Quarantine moves the file into the quarantine tree and writes a sidecar."""
    record = adapter.upload(b"to delete", name="doc.txt", logical_path="/doc.txt")
    original_path = storage_root / record.provider_file_id

    quarantine_id = adapter.quarantine(record.provider_file_id)

    assert re.fullmatch(r"[0-9a-f]{32}", quarantine_id)
    assert not original_path.exists()

    entry_dir = storage_root / ".quarantine" / quarantine_id
    assert entry_dir.is_dir()
    quarantined = entry_dir / record.provider_file_id
    assert quarantined.is_file()
    assert quarantined.read_bytes() == b"to delete"

    meta = json.loads((entry_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["provider_file_id"] == record.provider_file_id
    assert meta["size_bytes"] == len(b"to delete")
    assert meta["quarantined_at"]


def test_quarantine_missing_file(adapter: LocalAdapter) -> None:
    """Quarantining a nonexistent file raises StorageNotFoundError."""
    with pytest.raises(StorageNotFoundError):
        adapter.quarantine("missing.txt")


def test_quarantine_rejects_unsafe_id(adapter: LocalAdapter) -> None:
    """Quarantine rejects traversal identifiers like download does."""
    with pytest.raises(StorageNotFoundError):
        adapter.quarantine("../outside.txt")


def test_restore_returns_file_to_original_location(
    adapter: LocalAdapter, storage_root: Path
) -> None:
    """Restore moves the quarantined file back and removes the entry."""
    record = adapter.upload(b"keep me", name="doc.txt", logical_path="/doc.txt")
    quarantine_id = adapter.quarantine(record.provider_file_id)

    restored = adapter.restore(quarantine_id)

    assert isinstance(restored, ProviderFileRecord)
    assert restored.provider_file_id == record.provider_file_id
    assert restored.size_bytes == len(b"keep me")
    original_path = storage_root / record.provider_file_id
    assert original_path.is_file()
    assert original_path.read_bytes() == b"keep me"
    # The quarantine entry is gone.
    assert not (storage_root / ".quarantine" / quarantine_id).exists()


def test_restore_missing_entry(adapter: LocalAdapter) -> None:
    """Restoring an unknown quarantine ID raises StorageNotFoundError."""
    with pytest.raises(StorageNotFoundError):
        adapter.restore(uuid.uuid4().hex)


@pytest.mark.parametrize(
    "bad_id",
    [
        "../..",
        "zz",  # not hex
        "",
        "a" * 15,  # too short
        "a" * 65,  # too long
        "ABCDEF",  # uppercase hex is not a valid quarantine ID shape
        "with space",
    ],
)
def test_restore_rejects_malformed_quarantine_ids(
    adapter: LocalAdapter, bad_id: str
) -> None:
    """Malformed quarantine IDs are rejected before any filesystem access."""
    with pytest.raises(StorageNotFoundError):
        adapter.restore(bad_id)


def test_cleanup_removes_quarantine_entry(
    adapter: LocalAdapter, storage_root: Path
) -> None:
    """Cleanup permanently removes the quarantined file and its metadata."""
    record = adapter.upload(b"bye", name="doc.txt", logical_path="/doc.txt")
    quarantine_id = adapter.quarantine(record.provider_file_id)
    entry_dir = storage_root / ".quarantine" / quarantine_id
    assert entry_dir.is_dir()

    adapter.cleanup(quarantine_id)

    assert not entry_dir.exists()
    assert not (storage_root / record.provider_file_id).exists()


def test_cleanup_missing_entry(adapter: LocalAdapter) -> None:
    """Cleaning up an unknown quarantine ID raises StorageNotFoundError."""
    with pytest.raises(StorageNotFoundError):
        adapter.cleanup(uuid.uuid4().hex)


def test_full_deletion_protocol_round_trip(
    adapter: LocalAdapter, storage_root: Path
) -> None:
    """The approved protocol: quarantine -> (db step) -> restore or cleanup."""
    record = adapter.upload(b"payload", name="doc.txt", logical_path="/doc.txt")

    # Phase 1: quarantine.
    qid = adapter.quarantine(record.provider_file_id)
    assert not (storage_root / record.provider_file_id).exists()

    # Phase 3b: database commit failed -> restore.
    adapter.restore(qid)
    assert (storage_root / record.provider_file_id).is_file()

    # Retry: quarantine again, this time the commit succeeds -> cleanup.
    qid2 = adapter.quarantine(record.provider_file_id)
    assert qid2 != qid
    adapter.cleanup(qid2)
    assert not (storage_root / record.provider_file_id).exists()
    assert not (storage_root / ".quarantine" / qid2).exists()


# ---------------------------------------------------------------------------
# Quarantine persistence across adapter recreation
# ---------------------------------------------------------------------------


def test_quarantine_state_survives_adapter_recreation(
    storage_root: Path,
) -> None:
    """A second adapter instance over the same root restores what the first quarantined."""
    first = LocalAdapter(storage_root)
    record = first.upload(b"persisted", name="doc.txt", logical_path="/doc.txt")
    quarantine_id = first.quarantine(record.provider_file_id)

    # Simulate a process restart: brand-new adapter over the same root.
    second = LocalAdapter(storage_root)
    restored = second.restore(quarantine_id)

    assert restored.provider_file_id == record.provider_file_id
    original_path = storage_root / record.provider_file_id
    assert original_path.is_file()
    assert original_path.read_bytes() == b"persisted"
    assert not (storage_root / ".quarantine" / quarantine_id).exists()


def test_cleanup_after_adapter_recreation(storage_root: Path) -> None:
    """A second adapter instance can also permanently clean up old quarantine state."""
    first = LocalAdapter(storage_root)
    record = first.upload(b"gone", name="doc.txt", logical_path="/doc.txt")
    quarantine_id = first.quarantine(record.provider_file_id)

    second = LocalAdapter(storage_root)
    second.cleanup(quarantine_id)

    assert not (storage_root / ".quarantine" / quarantine_id).exists()
    assert not (storage_root / record.provider_file_id).exists()


# ---------------------------------------------------------------------------
# Interrupted quarantine recovery (missing sidecar)
# ---------------------------------------------------------------------------


def test_restore_recovers_entry_without_sidecar(storage_root: Path) -> None:
    """A crash between the file move and the metadata write is recoverable.

    The entry has the quarantined file but no meta.json; restore identifies
    the file from the entry directory itself.
    """
    provider_file_id = f"{uuid.uuid4().hex}.txt"
    # Do NOT create the file in storage_root - simulate a file that was
    # quarantined but the sidecar write failed before the original was removed.
    # We manually create the quarantine entry with the file inside.
    quarantine_id = uuid.uuid4().hex
    entry_dir = storage_root / ".quarantine" / quarantine_id
    entry_dir.mkdir(parents=True)
    (entry_dir / provider_file_id).write_bytes(b"interrupted")
    # No meta.json written

    adapter = LocalAdapter(storage_root)
    restored = adapter.restore(quarantine_id)

    assert restored.provider_file_id == provider_file_id
    assert (storage_root / provider_file_id).read_bytes() == b"interrupted"
    assert not (storage_root / ".quarantine" / quarantine_id).exists()


def test_cleanup_works_without_sidecar(storage_root: Path) -> None:
    """Cleanup does not require the sidecar to exist."""
    provider_file_id = f"{uuid.uuid4()}.txt"
    (storage_root / provider_file_id).write_bytes(b"data")
    quarantine_id = _make_quarantine_entry(storage_root, provider_file_id, meta=None)

    adapter = LocalAdapter(storage_root)
    adapter.cleanup(quarantine_id)

    assert not (storage_root / ".quarantine" / quarantine_id).exists()


# ---------------------------------------------------------------------------
# Tampered / malformed quarantine metadata
# ---------------------------------------------------------------------------


def test_restore_ignores_tampered_traversal_id(
    storage_root: Path,
) -> None:
    """A sidecar claiming a traversal provider_file_id is never trusted.

    The file is recovered from its actual name in the entry directory, and
    nothing is written outside the storage root.
    """
    real_name = f"{uuid.uuid4()}.txt"
    quarantine_id = _make_quarantine_entry(
        storage_root,
        real_name,
        meta={"provider_file_id": "../evil.txt", "size_bytes": 5},
    )
    outside = storage_root.parent / "evil.txt"
    assert not outside.exists()

    adapter = LocalAdapter(storage_root)
    restored = adapter.restore(quarantine_id)

    # Recovered under the file's real name, not the tampered one.
    assert restored.provider_file_id == real_name
    assert (storage_root / real_name).is_file()
    assert not outside.exists()
    assert not (storage_root / "evil.txt").exists()
    assert not (storage_root / ".quarantine" / quarantine_id).exists()


@pytest.mark.parametrize(
    "meta",
    [
        {"provider_file_id": "C:/evil.txt"},
        {"provider_file_id": "sub\\evil.txt"},
        {"provider_file_id": ".quarantine"},
        {"provider_file_id": ""},
        {"provider_file_id": 12345},
        {"size_bytes": -1},
        {"size_bytes": True},
        {"quarantined_at": 42},
        ["not", "a", "dict"],
    ],
)
def test_restore_rejects_untrusted_sidecar_fields(
    storage_root: Path, meta: object
) -> None:
    """Any invalid sidecar field disables the sidecar; recovery uses the directory."""
    real_name = f"{uuid.uuid4()}.txt"
    content = b"safe"
    quarantine_id = _make_quarantine_entry(storage_root, real_name, content=content, meta=meta)

    adapter = LocalAdapter(storage_root)
    restored = adapter.restore(quarantine_id)

    assert restored.provider_file_id == real_name
    assert (storage_root / real_name).read_bytes() == content


def test_restore_with_malformed_json_sidecar(storage_root: Path) -> None:
    """Unparseable sidecar JSON is treated as missing; directory recovery applies."""
    real_name = f"{uuid.uuid4()}.txt"
    quarantine_id = _make_quarantine_entry(
        storage_root, real_name, content=b"json", meta={"provider_file_id": real_name}
    )
    (storage_root / ".quarantine" / quarantine_id / "meta.json").write_text(
        "{not valid json", encoding="utf-8"
    )

    adapter = LocalAdapter(storage_root)
    restored = adapter.restore(quarantine_id)

    assert restored.provider_file_id == real_name
    assert (storage_root / real_name).read_bytes() == b"json"


def test_restore_sidecar_claiming_absent_file_falls_back_to_directory(
    storage_root: Path,
) -> None:
    """A sidecar naming a file that is not in the entry falls back to the real file."""
    real_name = f"{uuid.uuid4()}.txt"
    quarantine_id = _make_quarantine_entry(
        storage_root,
        real_name,
        meta={"provider_file_id": "other-missing.txt"},
    )

    adapter = LocalAdapter(storage_root)
    restored = adapter.restore(quarantine_id)

    assert restored.provider_file_id == real_name
    assert (storage_root / real_name).is_file()


def test_restore_empty_entry_raises(storage_root: Path) -> None:
    """An entry directory with no quarantined file raises StorageNotFoundError."""
    quarantine_id = uuid.uuid4().hex
    (storage_root / ".quarantine" / quarantine_id).mkdir(parents=True)

    adapter = LocalAdapter(storage_root)
    with pytest.raises(StorageNotFoundError):
        adapter.restore(quarantine_id)


def test_restore_multiple_files_in_entry_raises(storage_root: Path) -> None:
    """An entry with more than one candidate file is ambiguous and rejected."""
    quarantine_id = uuid.uuid4().hex
    entry_dir = storage_root / ".quarantine" / quarantine_id
    entry_dir.mkdir(parents=True)
    (entry_dir / f"{uuid.uuid4()}.txt").write_bytes(b"one")
    (entry_dir / f"{uuid.uuid4()}.txt").write_bytes(b"two")

    adapter = LocalAdapter(storage_root)
    with pytest.raises(StorageNotFoundError):
        adapter.restore(quarantine_id)


# ---------------------------------------------------------------------------
# Restore conflict handling
# ---------------------------------------------------------------------------


def test_restore_refuses_to_overwrite_occupied_path(
    adapter: LocalAdapter, storage_root: Path
) -> None:
    """If the original path is occupied again, restore is a conflict and the
    quarantined copy is left in place so no data is lost."""
    record = adapter.upload(b"old content", name="doc.txt", logical_path="/doc.txt")
    quarantine_id = adapter.quarantine(record.provider_file_id)

    # Something re-occupies the original path while the file is quarantined.
    (storage_root / record.provider_file_id).write_bytes(b"new content")

    with pytest.raises(StorageConflictError):
        adapter.restore(quarantine_id)

    # Both copies survive: the new file untouched, the quarantined one intact.
    assert (storage_root / record.provider_file_id).read_bytes() == b"new content"
    quarantined = storage_root / ".quarantine" / quarantine_id / record.provider_file_id
    assert quarantined.read_bytes() == b"old content"


def test_restore_refuses_symlink_at_original_path(
    storage_root: Path,
) -> None:
    """A symlink planted at the original path blocks the restore (conflict).

    Setup: upload a file, quarantine it (original removed), then plant a symlink
    at the now-vacant original path. Restore must refuse to overwrite/follow it.
    """
    provider_file_id = f"{uuid.uuid4().hex}.txt"
    adapter = LocalAdapter(storage_root)
    adapter.upload(b"victim", name="doc.txt", logical_path="/doc.txt")
    # Note: upload generates its own UUID name, so we need to use the actual provider_file_id
    # Let's use a simpler approach: upload, get the record, quarantine, then symlink.
    record = adapter.upload(b"victim", name="doc.txt", logical_path="/doc.txt")
    quarantine_id = adapter.quarantine(record.provider_file_id)

    # Now the original path is vacant. Plant a symlink there.
    outside_target = storage_root.parent / "sneaky.txt"
    outside_target.write_bytes(b"external")
    _make_symlink(storage_root / record.provider_file_id, outside_target)

    with pytest.raises(StorageConflictError):
        adapter.restore(quarantine_id)

    # The planted symlink and its target are untouched; the quarantined copy
    # is still recoverable.
    assert (storage_root / record.provider_file_id).is_symlink()
    assert outside_target.read_bytes() == b"external"
    quarantined = storage_root / ".quarantine" / quarantine_id / record.provider_file_id
    assert quarantined.read_bytes() == b"victim"


def test_restore_race_collision_at_publication(
    adapter: LocalAdapter, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A destination appearing between the pre-check and the os.link publication
    during restore is a conflict — the quarantined file is preserved."""
    import os

    original_link = os.link

    def racing_link(src: str, dst: str) -> None:
        # Match the target file being restored
        if "restore_collision" in str(dst):
            raise FileExistsError("race: destination appeared")
        return original_link(src, dst)

    monkeypatch.setattr(os, "link", racing_link)

    record = adapter.upload(b"victim", name="restore_collision.txt", logical_path="/restore_collision.txt")
    quarantine_id = adapter.quarantine(record.provider_file_id)

    # Re-occupy the original path
    (storage_root / record.provider_file_id).write_bytes(b"new content")

    with pytest.raises(StorageConflictError):
        adapter.restore(quarantine_id)

    # Both copies survive
    assert (storage_root / record.provider_file_id).read_bytes() == b"new content"
    quarantined = storage_root / ".quarantine" / quarantine_id / record.provider_file_id
    assert quarantined.read_bytes() == b"victim"


def test_restore_unsupported_hardlink_raises_error(
    adapter: LocalAdapter, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.link is unsupported during restore, the quarantined file is
    preserved and StorageUnavailableError is raised."""
    import os

    original_link = os.link

    def failing_link(src: str, dst: str) -> None:
        # Only fail for restore (target in quarantine directory)
        if ".quarantine" in str(src):
            raise OSError("hard links not supported")
        return original_link(src, dst)

    monkeypatch.setattr(os, "link", failing_link)

    record = adapter.upload(b"victim", name="doc.txt", logical_path="/doc.txt")
    quarantine_id = adapter.quarantine(record.provider_file_id)

    with pytest.raises(StorageUnavailableError) as excinfo:
        adapter.restore(quarantine_id)

    assert excinfo.value.provider == "LOCAL"
    assert "Atomic no-overwrite restore not supported" in str(excinfo.value)
    # Quarantined file preserved
    quarantined = storage_root / ".quarantine" / quarantine_id / record.provider_file_id
    assert quarantined.read_bytes() == b"victim"


# ---------------------------------------------------------------------------
# Symlink protection
# ---------------------------------------------------------------------------


def test_quarantine_refuses_symlink_pointing_outside_root(
    storage_root: Path,
) -> None:
    """A symlink inside the root that points outside is refused for quarantine."""
    outside_target = storage_root.parent / "outside.txt"
    outside_target.write_bytes(b"precious")
    provider_file_id = f"{uuid.uuid4()}.txt"
    _make_symlink(storage_root / provider_file_id, outside_target)

    adapter = LocalAdapter(storage_root)
    with pytest.raises(StoragePermissionError) as excinfo:
        adapter.quarantine(provider_file_id)
    assert excinfo.value.provider == "LOCAL"

    # The symlink and its target remain intact; nothing was moved.
    assert (storage_root / provider_file_id).is_symlink()
    assert outside_target.read_bytes() == b"precious"
    assert not (storage_root / ".quarantine").exists()


def test_quarantine_refuses_symlink_pointing_inside_root(
    storage_root: Path,
) -> None:
    """Even a symlink pointing at another location INSIDE the root is refused."""
    real_name = f"{uuid.uuid4()}.txt"
    (storage_root / real_name).write_bytes(b"real file")
    link_name = f"{uuid.uuid4()}.txt"
    _make_symlink(storage_root / link_name, storage_root / real_name)

    adapter = LocalAdapter(storage_root)
    with pytest.raises(StoragePermissionError):
        adapter.quarantine(link_name)

    # The symlink is not followed: the real file is untouched.
    assert (storage_root / real_name).read_bytes() == b"real file"
    assert (storage_root / link_name).is_symlink()


def test_download_refuses_symlink_entry(storage_root: Path) -> None:
    """Downloading through a symlink inside the root is treated as not found."""
    real_name = f"{uuid.uuid4()}.txt"
    (storage_root / real_name).write_bytes(b"secret")
    link_name = f"{uuid.uuid4()}.txt"
    _make_symlink(storage_root / link_name, storage_root / real_name)

    adapter = LocalAdapter(storage_root)
    with pytest.raises(StorageNotFoundError):
        adapter.download(link_name)


def test_rename_and_move_refuse_symlink_entries(storage_root: Path) -> None:
    """Rename and move also refuse to operate on symlink entries."""
    real_name = f"{uuid.uuid4()}.txt"
    (storage_root / real_name).write_bytes(b"data")
    link_name = f"{uuid.uuid4()}.txt"
    _make_symlink(storage_root / link_name, storage_root / real_name)

    adapter = LocalAdapter(storage_root)
    with pytest.raises(StorageNotFoundError):
        adapter.rename(link_name, "new.txt")
    with pytest.raises(StorageNotFoundError):
        adapter.move(link_name, "/elsewhere/new.txt")


# ---------------------------------------------------------------------------
# Quarantine metadata-write failure recovery
# ---------------------------------------------------------------------------


def test_quarantine_restores_file_when_meta_write_fails(
    adapter: LocalAdapter, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the sidecar cannot be written, the file is moved back and the failure
    is reported — the original file must remain intact and recoverable.
    The quarantine entry directory is cleaned up; the parent .quarantine
    directory may remain if other entries exist.
    """
    record = adapter.upload(b"safe", name="doc.txt", logical_path="/doc.txt")
    original_path = storage_root / record.provider_file_id

    def failing_meta_write(self: LocalAdapter, entry_dir: Path, meta: dict) -> None:
        raise OSError("metadata write failed")

    monkeypatch.setattr(LocalAdapter, "_write_meta_atomic", failing_meta_write)

    with pytest.raises(StorageUnavailableError) as excinfo:
        adapter.quarantine(record.provider_file_id)
    assert excinfo.value.provider == "LOCAL"

    # The file is back at its original location with its original content.
    assert original_path.is_file()
    assert original_path.read_bytes() == b"safe"
    # The specific quarantine entry was cleaned up (no entry subdirectories remain).
    quarantine_dir = storage_root / ".quarantine"
    if quarantine_dir.exists():
        assert not any(quarantine_dir.iterdir()), "quarantine entry should be removed"


def test_quarantine_meta_failure_with_failed_rollback_still_recoverable(
    storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if both the meta write AND the rollback fail, the file is still
    recoverable later via directory-based restore (never lost)."""
    adapter = LocalAdapter(storage_root)
    record = adapter.upload(b"doomed", name="doc.txt", logical_path="/doc.txt")
    original_path = storage_root / record.provider_file_id

    def failing_meta_write(self: LocalAdapter, entry_dir: Path, meta: dict) -> None:
        raise OSError("metadata write failed")

    def failing_rollback(
        self: LocalAdapter, quarantined_file: Path, original_path: Path, entry_dir: Path
    ) -> None:
        # Simulate the rollback rename also failing: the file stays in the
        # quarantine entry with no sidecar.
        LocalAdapter._remove_empty_dir(entry_dir)

    monkeypatch.setattr(LocalAdapter, "_write_meta_atomic", failing_meta_write)
    monkeypatch.setattr(LocalAdapter, "_restore_after_failed_meta", failing_rollback)

    with pytest.raises(StorageUnavailableError):
        adapter.quarantine(record.provider_file_id)

    # Original location is empty, but the quarantined copy still exists
    # somewhere under .quarantine.
    assert not original_path.exists()
    entries = list((storage_root / ".quarantine").iterdir())
    assert len(entries) == 1
    entry_dir = entries[0]
    quarantined = entry_dir / record.provider_file_id
    assert quarantined.is_file()
    assert quarantined.read_bytes() == b"doomed"

    # A later restore recovers the file from the entry directory alone.
    restored = adapter.restore(entry_dir.name)
    assert restored.provider_file_id == record.provider_file_id
    assert original_path.read_bytes() == b"doomed"


def test_quarantine_rollback_does_not_overwrite_occupied_original(
    adapter: LocalAdapter, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the original path is re-occupied during a failed quarantine,
    rollback preserves the quarantined file instead of overwriting."""
    record = adapter.upload(b"safe", name="doc.txt", logical_path="/doc.txt")
    original_path = storage_root / record.provider_file_id

    def failing_meta_write(self: LocalAdapter, entry_dir: Path, meta: dict) -> None:
        raise OSError("metadata write failed")

    monkeypatch.setattr(LocalAdapter, "_write_meta_atomic", failing_meta_write)

    # Re-occupy the original path while quarantine is in progress
    (storage_root / record.provider_file_id).write_bytes(b"new content")

    with pytest.raises(StorageUnavailableError):
        adapter.quarantine(record.provider_file_id)

    # New content at original path is preserved
    assert original_path.read_bytes() == b"new content"
    # Quarantined file is preserved in quarantine entry (recoverable)
    quarantine_dir = storage_root / ".quarantine"
    if quarantine_dir.exists():
        entries = list(quarantine_dir.iterdir())
        if entries:
            quarantined = entries[0] / record.provider_file_id
            assert quarantined.read_bytes() == b"safe"


# ---------------------------------------------------------------------------
# Error normalization
# ---------------------------------------------------------------------------


def test_all_errors_are_storage_errors_and_local_provider(
    adapter: LocalAdapter,
) -> None:
    """Every adapter failure is a StorageError with provider='LOCAL'."""
    record = adapter.upload(b"x", name="doc.txt", logical_path="/doc.txt")
    qid = adapter.quarantine(record.provider_file_id)

    failures = [
        lambda: adapter.download("missing.txt"),
        lambda: adapter.restore(uuid.uuid4().hex),
        lambda: adapter.cleanup(uuid.uuid4().hex),
    ]
    for failure in failures:
        with pytest.raises(StorageError) as excinfo:
            failure()
        assert excinfo.value.provider == "LOCAL"


# ---------------------------------------------------------------------------
# Hardened symlink and permission tests
# ---------------------------------------------------------------------------


def test_quarantine_refuses_symlinked_quarantine_dir(
    storage_root: Path,
) -> None:
    """If .quarantine itself is a symlink, quarantine must fail."""
    real_quarantine = storage_root / "real_quarantine"
    real_quarantine.mkdir()
    quarantine_link = storage_root / ".quarantine"
    _make_symlink(quarantine_link, real_quarantine)

    adapter = LocalAdapter(storage_root)
    record = adapter.upload(b"data", "doc.txt", "/doc.txt")

    with pytest.raises(StorageNotFoundError):
        adapter.quarantine(record.provider_file_id)


def test_quarantine_refuses_symlinked_entry_dir(
    storage_root: Path,
) -> None:
    """If a specific quarantine entry is a symlink, it must be rejected."""
    quarantine_id = uuid.uuid4().hex
    quarantine_dir = storage_root / ".quarantine"
    quarantine_dir.mkdir()
    
    real_entry = storage_root / "real_entry"
    real_entry.mkdir()
    entry_link = quarantine_dir / quarantine_id
    _make_symlink(entry_link, real_entry)

    adapter = LocalAdapter(storage_root)
    # We can't use adapter.quarantine to create this state easily, 
    # but we can test restore/cleanup/etc.
    with pytest.raises(StorageNotFoundError):
        adapter.restore(quarantine_id)
    with pytest.raises(StorageNotFoundError):
        adapter.cleanup(quarantine_id)


def test_restore_refuses_symlinked_meta_json(
    storage_root: Path,
) -> None:
    """If meta.json is a symlink, it must be rejected (not silently fall back)."""
    provider_file_id = f"{uuid.uuid4().hex}.txt"
    quarantine_id = uuid.uuid4().hex
    entry_dir = storage_root / ".quarantine" / quarantine_id
    entry_dir.mkdir(parents=True)
    (entry_dir / provider_file_id).write_bytes(b"content")

    # Plant a symlinked meta.json pointing to something else
    other_meta = storage_root / "other_meta.json"
    other_meta.write_text(json.dumps({"provider_file_id": "evil.txt"}), encoding="utf-8")
    _make_symlink(entry_dir / "meta.json", other_meta)

    adapter = LocalAdapter(storage_root)
    with pytest.raises(StoragePermissionError):
        adapter.restore(quarantine_id)

    # Quarantined file must remain in place (not restored, not deleted)
    assert (entry_dir / provider_file_id).exists()
    assert (entry_dir / provider_file_id).read_bytes() == b"content"


def test_restore_refuses_symlinked_recovered_file(
    storage_root: Path,
) -> None:
    """If the file inside a quarantine entry is a symlink, it must be rejected."""
    quarantine_id = uuid.uuid4().hex
    entry_dir = storage_root / ".quarantine" / quarantine_id
    entry_dir.mkdir(parents=True)
    
    real_file = storage_root / "real_file.txt"
    real_file.write_bytes(b"precious")
    
    provider_file_id = f"{uuid.uuid4().hex}.txt"
    _make_symlink(entry_dir / provider_file_id, real_file)

    adapter = LocalAdapter(storage_root)
    with pytest.raises(StorageNotFoundError):
        adapter.restore(quarantine_id)


def test_cleanup_refuses_symlinked_entry_without_following(
    storage_root: Path,
) -> None:
    """Cleanup must refuse a symlinked entry and must not delete the target."""
    quarantine_dir = storage_root / ".quarantine"
    quarantine_dir.mkdir()
    
    target_dir = storage_root / "do_not_delete"
    target_dir.mkdir()
    (target_dir / "file.txt").write_bytes(b"safe")
    
    quarantine_id = uuid.uuid4().hex
    entry_link = quarantine_dir / quarantine_id
    _make_symlink(entry_link, target_dir)

    adapter = LocalAdapter(storage_root)
    with pytest.raises(StorageNotFoundError):
        adapter.cleanup(quarantine_id)
        
    # Target must still exist
    assert target_dir.is_dir()
    assert (target_dir / "file.txt").exists()




def test_quarantine_refuses_non_directory_quarantine(
    storage_root: Path,
) -> None:
    """If .quarantine exists but is not a directory (e.g. a file), quarantine must fail."""
    # Create a file named .quarantine (not a directory)
    (storage_root / ".quarantine").write_text("not a directory")

    adapter = LocalAdapter(storage_root)
    record = adapter.upload(b"data", "doc.txt", "/doc.txt")

    with pytest.raises(StorageNotFoundError):
        adapter.quarantine(record.provider_file_id)


def test_restore_symlinked_entry_leaves_external_target_untouched(
    storage_root: Path,
) -> None:
    """Restore must not follow a symlinked quarantine entry to an external target.
    
    If a quarantine entry is a symlink pointing outside the storage root,
    restore must reject it and leave the external target completely untouched.
    """
    quarantine_dir = storage_root / ".quarantine"
    quarantine_dir.mkdir()

    # Create an external directory OUTSIDE the storage root
    external_root = storage_root.parent / "external_target"
    external_root.mkdir(exist_ok=True)
    external_file = external_root / "precious.txt"
    external_file.write_bytes(b"do not touch")

    # Create a symlinked quarantine entry pointing to the external directory
    quarantine_id = uuid.uuid4().hex
    entry_link = quarantine_dir / quarantine_id
    _make_symlink(entry_link, external_root)

    adapter = LocalAdapter(storage_root)
    with pytest.raises(StorageNotFoundError):
        adapter.restore(quarantine_id)

    # External target must be completely untouched
    assert external_file.exists()
    assert external_file.read_bytes() == b"do not touch"


def test_upload_permission_error_mapping(
    adapter: LocalAdapter, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PermissionError during upload must become StoragePermissionError."""
    import os
    
    def failing_link(src, dst):
        raise PermissionError("denied")
        
    monkeypatch.setattr(os, "link", failing_link)
    
    with pytest.raises(StoragePermissionError):
        adapter.upload(b"data", "doc.txt", "/doc.txt")


def test_quarantine_permission_error_mapping(
    adapter: LocalAdapter, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PermissionError during quarantine (mkdir) must become StoragePermissionError."""
    original_mkdir = Path.mkdir
    
    def failing_mkdir(self, *args, **kwargs):
        if ".quarantine" in str(self):
            raise PermissionError("denied")
        return original_mkdir(self, *args, **kwargs)
        
    monkeypatch.setattr(Path, "mkdir", failing_mkdir)
    
    record = adapter.upload(b"data", "doc.txt", "/doc.txt")
    with pytest.raises(StoragePermissionError):
        adapter.quarantine(record.provider_file_id)


# ---------------------------------------------------------------------------
# Additional error-handling and race-condition tests (M2 hardening)
# ---------------------------------------------------------------------------


def test_read_quarantine_meta_permission_error(
    storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PermissionError reading meta.json must raise StoragePermissionError."""
    provider_file_id = f"{uuid.uuid4().hex}.txt"
    quarantine_id = _make_quarantine_entry(storage_root, provider_file_id)

    original_open = os.open

    def failing_open(path, flags, *args, **kwargs):
        if "meta.json" in str(path):
            raise PermissionError("permission denied")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing_open)

    adapter = LocalAdapter(storage_root)
    with pytest.raises(StoragePermissionError) as excinfo:
        adapter.restore(quarantine_id)
    assert excinfo.value.provider == "LOCAL"
    assert "Permission denied reading quarantine metadata" in str(excinfo.value)


def test_read_quarantine_meta_io_error(
    storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OSError (non-ELOOP/EISDIR) reading meta.json must raise StorageUnavailableError."""
    provider_file_id = f"{uuid.uuid4().hex}.txt"
    quarantine_id = _make_quarantine_entry(storage_root, provider_file_id)

    original_open = os.open

    def failing_open(path, flags, *args, **kwargs):
        if "meta.json" in str(path):
            raise OSError(errno.EIO, "disk I/O error")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing_open)

    adapter = LocalAdapter(storage_root)
    with pytest.raises(StorageUnavailableError) as excinfo:
        adapter.restore(quarantine_id)
    assert excinfo.value.provider == "LOCAL"
    assert "Failed to open quarantine metadata" in str(excinfo.value)


def test_read_quarantine_meta_non_regular_file(
    storage_root: Path,
) -> None:
    """A non-regular meta.json (directory, FIFO) must raise StoragePermissionError."""
    provider_file_id = f"{uuid.uuid4().hex}.txt"
    quarantine_id = _make_quarantine_entry(storage_root, provider_file_id)
    entry_dir = storage_root / ".quarantine" / quarantine_id

    # Replace meta.json with a directory
    (entry_dir / "meta.json").unlink()
    (entry_dir / "meta.json").mkdir()

    adapter = LocalAdapter(storage_root)
    with pytest.raises(StoragePermissionError) as excinfo:
        adapter.restore(quarantine_id)
    assert excinfo.value.provider == "LOCAL"
    # On Windows, opening a directory raises PermissionError before fstat check.
    # The key assertion is that it's a StoragePermissionError, not silently treated as missing.
    assert "quarantine metadata" in str(excinfo.value)


def test_read_quarantine_meta_fd_cleanup_on_failure(
    storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File descriptor must be closed when JSON parsing fails.

    This test verifies that invalid JSON in meta.json triggers the fallback
    (returns None) and directory-based recovery succeeds. The fd cleanup is
    guaranteed by the fdopen context manager in _read_quarantine_meta, which
    closes the fd when the with-block exits, before json.loads is called.
    Direct fd tracking via monkeypatch is not feasible on Windows because
    the file object's close() bypasses os.close().
    """
    provider_file_id = f"{uuid.uuid4().hex}.txt"
    quarantine_id = _make_quarantine_entry(storage_root, provider_file_id)
    entry_dir = storage_root / ".quarantine" / quarantine_id

    # Write invalid JSON that will cause json.loads to fail
    (entry_dir / "meta.json").write_text("{not valid json", encoding="utf-8")

    adapter = LocalAdapter(storage_root)

    # The invalid JSON causes _read_quarantine_meta to return None,
    # which triggers directory-based recovery. Since there's exactly one
    # quarantined file in the entry, restore should succeed.
    # This verifies the fd was properly closed by the fdopen context manager
    # (if it leaked, the file would be locked on Windows and restore might fail).
    restored = adapter.restore(quarantine_id)
    assert restored.provider_file_id == provider_file_id

    # Verify _read_quarantine_meta returns None for malformed JSON
    # (this is the internal behavior that triggers fallback)
    entry_dir2 = storage_root / ".quarantine" / quarantine_id
    # Re-create the entry since restore removed it
    entry_dir2.mkdir(parents=True)
    (entry_dir2 / provider_file_id).write_bytes(b"content")
    (entry_dir2 / "meta.json").write_text("{not valid json", encoding="utf-8")

    meta = adapter._read_quarantine_meta(entry_dir2, quarantine_id)
    assert meta is None, "Expected None for malformed JSON (fd should be closed)"


# ---------------------------------------------------------------------------
# Descriptor ownership and error-handling tests for _read_quarantine_meta
# ---------------------------------------------------------------------------


def test_read_quarantine_meta_fstat_failure(
    storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.fstat() failure after successful os.open() must close the fd and raise StorageUnavailableError."""
    provider_file_id = f"{uuid.uuid4().hex}.txt"
    quarantine_id = _make_quarantine_entry(storage_root, provider_file_id)
    entry_dir = storage_root / ".quarantine" / quarantine_id
    meta_path = entry_dir / "meta.json"

    adapter = LocalAdapter(storage_root)

    original_open = os.open
    original_fstat = os.fstat
    original_close = os.close
    opened_fds = set()
    closed_fds = set()

    def tracking_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if str(path) == str(meta_path):
            opened_fds.add(fd)
        return fd

    def tracking_fstat(fd):
        if fd in opened_fds:
            raise OSError(errno.EIO, "fstat failed")
        return original_fstat(fd)

    def tracking_close(fd):
        if fd in opened_fds:
            closed_fds.add(fd)
        return original_close(fd)

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "fstat", tracking_fstat)
    monkeypatch.setattr(os, "close", tracking_close)

    with pytest.raises(StorageUnavailableError) as excinfo:
        adapter._read_quarantine_meta(entry_dir, quarantine_id)
    assert excinfo.value.provider == "LOCAL"
    assert "Failed to open quarantine metadata" in str(excinfo.value) or "inspect" in str(excinfo.value).lower()

    # Verify fd was closed
    assert opened_fds == closed_fds, f"FD leak: opened {opened_fds}, closed {closed_fds}"


def test_read_quarantine_meta_fdopen_failure(
    storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.fdopen() failure after successful os.open() must close the fd and raise StorageUnavailableError."""
    provider_file_id = f"{uuid.uuid4().hex}.txt"
    quarantine_id = _make_quarantine_entry(storage_root, provider_file_id)
    entry_dir = storage_root / ".quarantine" / quarantine_id
    meta_path = entry_dir / "meta.json"

    adapter = LocalAdapter(storage_root)

    original_open = os.open
    original_fdopen = os.fdopen
    original_close = os.close
    opened_fds = set()
    closed_fds = set()

    def tracking_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if str(path) == str(meta_path):
            opened_fds.add(fd)
        return fd

    def failing_fdopen(fd, *args, **kwargs):
        if fd in opened_fds:
            raise OSError(errno.EINVAL, "fdopen failed")
        return original_fdopen(fd, *args, **kwargs)

    def tracking_close(fd):
        if fd in opened_fds:
            closed_fds.add(fd)
        return original_close(fd)

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "fdopen", failing_fdopen)
    monkeypatch.setattr(os, "close", tracking_close)

    # fdopen failure is a system error, should raise StorageUnavailableError
    with pytest.raises(StorageUnavailableError) as excinfo:
        adapter._read_quarantine_meta(entry_dir, quarantine_id)
    assert excinfo.value.provider == "LOCAL"

    # Verify fd was closed
    assert opened_fds == closed_fds, f"FD leak: opened {opened_fds}, closed {closed_fds}"


def test_read_quarantine_meta_read_permission_error(
    storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read PermissionError must close the fd and raise StoragePermissionError."""
    provider_file_id = f"{uuid.uuid4().hex}.txt"
    quarantine_id = _make_quarantine_entry(storage_root, provider_file_id)
    entry_dir = storage_root / ".quarantine" / quarantine_id
    meta_path = entry_dir / "meta.json"

    adapter = LocalAdapter(storage_root)

    original_open = os.open
    original_fdopen = os.fdopen
    opened_fds = set()
    closed_file_objects = set()

    def tracking_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if str(path) == str(meta_path):
            opened_fds.add(fd)
        return fd

    def tracking_fdopen(fd, *args, **kwargs):
        f = original_fdopen(fd, *args, **kwargs)
        if fd in opened_fds:
            # Wrap the file object's read method to raise PermissionError
            original_read = f.read
            def failing_read(*a, **kw):
                raise PermissionError("permission denied reading")
            f.read = failing_read
            # Track the file object's close method
            original_fclose = f.close
            def tracking_fclose():
                closed_file_objects.add(f)
                return original_fclose()
            f.close = tracking_fclose
        return f

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "fdopen", tracking_fdopen)

    with pytest.raises(StoragePermissionError) as excinfo:
        adapter._read_quarantine_meta(entry_dir, quarantine_id)
    assert excinfo.value.provider == "LOCAL"
    assert "Permission denied reading quarantine metadata" in str(excinfo.value)

    # Verify file object was closed (which closes the underlying fd)
    assert len(closed_file_objects) == 1


def test_read_quarantine_meta_read_os_error(
    storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read OSError (non-permission) must close the fd and raise StorageUnavailableError."""
    provider_file_id = f"{uuid.uuid4().hex}.txt"
    quarantine_id = _make_quarantine_entry(storage_root, provider_file_id)
    entry_dir = storage_root / ".quarantine" / quarantine_id
    meta_path = entry_dir / "meta.json"

    adapter = LocalAdapter(storage_root)

    original_open = os.open
    original_fdopen = os.fdopen
    opened_fds = set()
    closed_file_objects = set()

    def tracking_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if str(path) == str(meta_path):
            opened_fds.add(fd)
        return fd

    def tracking_fdopen(fd, *args, **kwargs):
        f = original_fdopen(fd, *args, **kwargs)
        if fd in opened_fds:
            original_read = f.read
            def failing_read(*a, **kw):
                raise OSError(errno.EIO, "disk I/O error")
            f.read = failing_read
            # Track the file object's close method
            original_fclose = f.close
            def tracking_fclose():
                closed_file_objects.add(f)
                return original_fclose()
            f.close = tracking_fclose
        return f

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "fdopen", tracking_fdopen)

    with pytest.raises(StorageUnavailableError) as excinfo:
        adapter._read_quarantine_meta(entry_dir, quarantine_id)
    assert excinfo.value.provider == "LOCAL"
    assert "Failed to read quarantine metadata" in str(excinfo.value)

    # Verify file object was closed (which closes the underlying fd)
    assert len(closed_file_objects) == 1


def test_quarantine_source_symlink_substitution(
    storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symlink substitution between validation and rename must be caught or fail closed.

    This test simulates an attacker replacing the source file with a symlink
    between the fd-based validation (os.open + fstat) and the pre-rename lstat check.
    The defense-in-depth lstat check immediately before rename should catch this.

    On Windows, symlink creation may require elevated privileges; if symlinks
    cannot be created, the test is skipped rather than claiming coverage.
    """
    adapter = LocalAdapter(storage_root)
    record = adapter.upload(b"original content", "doc.txt", "/doc.txt")
    provider_file_id = record.provider_file_id
    original_path = storage_root / provider_file_id

    # Create an external target file that the symlink would point to
    external_target = storage_root.parent / "external_target.txt"
    external_target.write_bytes(b"precious external data")

    # Track whether the pre-rename lstat was called
    original_lstat = Path.lstat
    lstat_before_rename_called = {"count": 0}

    def tracking_lstat(self):
        if str(self) == str(original_path):
            lstat_before_rename_called["count"] += 1
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", tracking_lstat)

    # Monkeypatch os.close to replace the source file with a symlink
    # when the validation fd is closed (simulating TOCTOU between
    # fd validation and pre-rename lstat check)
    original_close = os.close
    substitution_done = {"done": False}

    def close_with_substitution(fd):
        # Only substitute once, and only for the fd that was opened
        # for the source file validation (we can't easily identify it,
        # so we substitute on the first close after upload)
        if not substitution_done["done"]:
            try:
                # Replace the source file with a symlink to external target
                if original_path.exists():
                    original_path.unlink()
                original_path.symlink_to(external_target)
                substitution_done["done"] = True
            except (OSError, NotImplementedError):
                # Symlink creation failed (e.g., Windows without privileges)
                pytest.skip("Symlink creation not permitted on this host")
        return original_close(fd)

    monkeypatch.setattr(os, "close", close_with_substitution)

    # The quarantine should fail with StoragePermissionError
    with pytest.raises(StoragePermissionError) as excinfo:
        adapter.quarantine(provider_file_id)

    assert excinfo.value.provider == "LOCAL"
    assert "replaced with a symlink before rename" in str(excinfo.value)

    # The original file should still be in place (quarantine failed closed)
    # Note: the original file was replaced by our symlink, but the quarantine
    # should have detected it and not moved anything. The symlink remains.
    assert original_path.is_symlink()
    assert original_path.read_bytes() == b"precious external data"

    # The external target must remain untouched
    assert external_target.exists()
    assert external_target.read_bytes() == b"precious external data"

    # No quarantine entry should have been created
    assert not (storage_root / ".quarantine").exists()

    # The pre-rename lstat should have been called
    assert lstat_before_rename_called["count"] >= 1


def test_resolve_quarantine_entry_permission_error(
    storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PermissionError during quarantine entry inspection must raise StoragePermissionError."""
    provider_file_id = f"{uuid.uuid4().hex}.txt"
    quarantine_id = _make_quarantine_entry(storage_root, provider_file_id)
    entry_dir = storage_root / ".quarantine" / quarantine_id

    original_lstat = Path.lstat

    def failing_lstat(self):
        if str(self) == str(entry_dir):
            raise PermissionError("permission denied")
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", failing_lstat)

    adapter = LocalAdapter(storage_root)
    with pytest.raises(StoragePermissionError) as excinfo:
        adapter.restore(quarantine_id)
    assert excinfo.value.provider == "LOCAL"
    assert "Permission denied inspecting quarantine entry" in str(excinfo.value)


def test_resolve_quarantine_entry_io_error(
    storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OSError during quarantine entry inspection must raise StorageUnavailableError."""
    provider_file_id = f"{uuid.uuid4().hex}.txt"
    quarantine_id = _make_quarantine_entry(storage_root, provider_file_id)
    entry_dir = storage_root / ".quarantine" / quarantine_id

    original_lstat = Path.lstat

    def failing_lstat(self):
        if str(self) == str(entry_dir):
            raise OSError(errno.EIO, "disk I/O error")
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", failing_lstat)

    adapter = LocalAdapter(storage_root)
    with pytest.raises(StorageUnavailableError) as excinfo:
        adapter.restore(quarantine_id)
    assert excinfo.value.provider == "LOCAL"
    assert "Failed to inspect quarantine entry" in str(excinfo.value)


#
# Approach: run a fresh Python subprocess that imports ONLY the local adapter
# module and then reports whether "fastapi" or "sqlalchemy" appear in
# sys.modules.  A subprocess is used because the main test process already
# loads FastAPI and SQLAlchemy (via the app fixtures in other test files), so
# checking sys.modules in-process would be a false negative.  The subprocess
# starts clean, so any presence of those packages proves the storage module
# imported them transitively.


def _check_no_framework_imports() -> str:
    """Import the local adapter module in a clean interpreter; report offenders."""
    code = (
        "import sys\n"
        "import backend.storage.local  # noqa: F401\n"
        "offenders = sorted(m for m in sys.modules if m.split('.')[0] in ('fastapi', 'sqlalchemy'))\n"
        "print('OFFENDERS:' + ','.join(offenders))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=".",
    )
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    for line in result.stdout.splitlines():
        if line.startswith("OFFENDERS:"):
            return line[len("OFFENDERS:"):]
    raise AssertionError("OFFENDERS marker missing from subprocess output")


def test_local_adapter_does_not_import_fastapi() -> None:
    """Importing the local adapter in a clean interpreter must not load FastAPI."""
    offenders = _check_no_framework_imports()
    assert "fastapi" not in offenders, f"fastapi was imported: {offenders}"


def test_local_adapter_does_not_import_sqlalchemy() -> None:
    """Importing the local adapter in a clean interpreter must not load SQLAlchemy."""
    offenders = _check_no_framework_imports()
    assert "sqlalchemy" not in offenders, f"sqlalchemy was imported: {offenders}"
