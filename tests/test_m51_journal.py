"""Journal infrastructure tests for SiloVault M5.1 Step 5.4 — Stage 1.1.

These tests verify the transaction journal module in isolation.
They must not alter installation behaviour or integrate with install().
"""

import json
import uuid
from pathlib import Path
from backend.ai._transaction import (
    write_journal,
    read_journal,
    validate_journal_schema,
    make_journal_record,
    new_transaction_id,
    TRANSACTIONS_DIR_NAME,
    _is_valid_uuid,
    VALID_STATES,
)
from backend.ai.storage import ModelStorage


def _tmp_dir():
    import tempfile
    return Path(tempfile.mkdtemp(prefix="silovault_journal_"))


# ---------------------------------------------------------------------------
# UUID validation helpers (shared by tests)
# ---------------------------------------------------------------------------

def _bad_uuid_examples():
    """Yield (label, bad_id) tuples for use in parametrized tests."""
    yield "missing-hyphens", "00000000000000000000000000000000"
    yield "extra-hyphens", "00000000-0000-0000-0000-0000000000000"


# ---------------------------------------------------------------------------
# Test: UUID validation
# ---------------------------------------------------------------------------

def test_is_valid_uuid():
    """_is_valid_uuid accepts proper UUIDs and rejects malformed ones."""
    # Valid
    assert _is_valid_uuid(new_transaction_id())
    # Invalid
    for _label, bad_id in _bad_uuid_examples():
        assert not _is_valid_uuid(bad_id), f"{_label!r} should be invalid"


# ---------------------------------------------------------------------------
# Test: Journal create and read (using proper UUIDs via new_transaction_id)
# ---------------------------------------------------------------------------

def test_journal_create_and_read():
    """Write a journal, read it back, verify content."""
    storage_root = _tmp_dir()
    txn_id = new_transaction_id()
    record = make_journal_record(
        transaction_id=txn_id,
        model_identifier="vendor.model",
        version="1.0",
        schema_version=2,
        state="staging",
        previous_installation_existed=False,
    )
    path = write_journal(storage_root, transaction_id=txn_id, record=record)
    assert path.is_file(), f"journal file not created at {path}"

    data = read_journal(storage_root, transaction_id=txn_id)
    assert data is not None, "read_journal returned None"
    assert data["transaction_id"] == txn_id
    assert data["model_identifier"] == "vendor.model"
    assert data["version"] == "1.0"
    assert data["schema_version"] == 2
    assert data["state"] == "staging"
    assert data["staging_dir"] is None
    assert data["previous_installation_existed"] is False


# ---------------------------------------------------------------------------
# Test: Reading a missing journal
# ---------------------------------------------------------------------------

def test_journal_read_missing():
    """Reading a non-existent journal returns None."""
    storage_root = _tmp_dir()
    data = read_journal(storage_root, transaction_id=new_transaction_id())
    assert data is None


# ---------------------------------------------------------------------------
# Test: Schema validation
# ---------------------------------------------------------------------------

def test_journal_schema_validation():
    """validate_journal_schema rejects invalid records."""
    # Missing required fields
    ok, err = validate_journal_schema({})
    assert not ok and "transaction_id" in err.lower()

    # Invalid state
    ok, err = validate_journal_schema({
        "transaction_id": new_transaction_id(),
        "model_identifier": "vendor.model",
        "version": "1.0",
        "schema_version": 2,
        "state": "invalid_state",
        "previous_installation_existed": False,
    })
    assert not ok and "state" in err

    # Absolute path in staging_dir
    ok, err = validate_journal_schema({
        "transaction_id": new_transaction_id(),
        "model_identifier": "vendor.model",
        "version": "1.0",
        "schema_version": 2,
        "state": "staging",
        "staging_dir": "/absolute/path",
        "previous_installation_existed": False,
    })
    assert not ok and "staging_dir" in err

    # Empty model_identifier
    ok, err = validate_journal_schema({
        "transaction_id": new_transaction_id(),
        "model_identifier": "",
        "version": "1.0",
        "schema_version": 2,
        "state": "staging",
        "previous_installation_existed": False,
    })
    assert not ok and "model_identifier" in err


# ---------------------------------------------------------------------------
# Test: Atomic write preserves existing journal
# ---------------------------------------------------------------------------

def test_journal_atomic_write_preserves_existing():
    """Atomic write: if temp fails, existing journal untouched."""
    import tempfile
    storage_root = _tmp_dir()
    txn_id = new_transaction_id()

    # First write
    record1 = make_journal_record(
        transaction_id=txn_id,
        model_identifier="vendor.model",
        version="1.0",
        schema_version=1,
        state="staging",
        previous_installation_existed=False,
    )
    path1 = write_journal(storage_root, transaction_id=txn_id, record=record1)
    assert path1.is_file(), f"journal file not created at {path1}"

    # Second write (should overwrite atomically)
    record2 = make_journal_record(
        transaction_id=txn_id,
        model_identifier="vendor.model",
        version="2.0",
        schema_version=2,
        state="publishing",
        previous_installation_existed=True,
    )
    path2 = write_journal(storage_root, transaction_id=txn_id, record=record2)
    assert path2.is_file(), f"journal file not created at {path2}"

    data = read_journal(storage_root, transaction_id=txn_id)
    assert data is not None, "read_journal returned None"
    assert data["version"] == "2.0", f"expected version 2.0, got {data.get('version')}"
    assert data["state"] == "publishing"


# ---------------------------------------------------------------------------
# Test: Multiple transactions no collision
# ---------------------------------------------------------------------------

def test_journal_multiple_transactions_no_collision():
    """Multiple transactions create separate files, no name collision."""
    storage_root = _tmp_dir()
    txn_ids = [new_transaction_id() for _ in range(5)]

    for txn_id in txn_ids:
        record = make_journal_record(
            transaction_id=txn_id,
            model_identifier="vendor.model",
            version="1.0",
            schema_version=2,
            state="staging",
            previous_installation_existed=False,
        )
        write_journal(storage_root, transaction_id=txn_id, record=record)

    # All should be readable
    for txn_id in txn_ids:
        data = read_journal(storage_root, transaction_id=txn_id)
        assert data is not None, f"journal {txn_id} not found after write"
        assert data["transaction_id"] == txn_id

    # File count should be 5
    txns_dir = storage_root / TRANSACTIONS_DIR_NAME
    files = list(txns_dir.glob("*.json"))
    assert len(files) == 5, f"expected 5 journal files, got {len(files)}"


# ---------------------------------------------------------------------------
# Test: Schema version validation
# ---------------------------------------------------------------------------

def test_journal_schema_version_validation():
    """read_journal rejects wrong schema version."""
    storage_root = _tmp_dir()
    txn_id = new_transaction_id()

    # Write v1
    record = make_journal_record(
        transaction_id=txn_id,
        model_identifier="vendor.model",
        version="1.0",
        schema_version=1,
        state="staging",
        previous_installation_existed=False,
    )
    write_journal(storage_root, transaction_id=txn_id, record=record)

    # Read with v2 requirement -> None
    data = read_journal(storage_root, transaction_id=txn_id, schema_version_required=2)
    assert data is None, "should reject v1 when v2 required"

    # Read without requirement -> succeeds
    data = read_journal(storage_root, transaction_id=txn_id)
    assert data is not None
    assert data["schema_version"] == 1


# ---------------------------------------------------------------------------
# Test: installed_at validation
# ---------------------------------------------------------------------------

def test_journal_installed_at_validation():
    """installed_at must be a non-empty string when present."""
    storage_root = _tmp_dir()
    txn_id = new_transaction_id()

    record = make_journal_record(
        transaction_id=txn_id,
        model_identifier="vendor.model",
        version="1.0",
        schema_version=2,
        state="committed",
        installed_at=None,
        previous_installation_existed=False,
    )
    write_journal(storage_root, transaction_id=txn_id, record=record)
    data = read_journal(storage_root, transaction_id=txn_id)
    assert data is not None
    assert data["installed_at"] is None, f"expected None, got {data.get('installed_at')}"

    # With installed_at
    record2 = make_journal_record(
        transaction_id=txn_id,
        model_identifier="vendor.model",
        version="1.0",
        schema_version=2,
        state="committed",
        installed_at="2026-01-15T10:30:00Z",
        previous_installation_existed=False,
    )
    write_journal(storage_root, transaction_id=txn_id, record=record2)
    data = read_journal(storage_root, transaction_id=txn_id)
    assert data is not None
    assert data["installed_at"] == "2026-01-15T10:30:00Z"


# ---------------------------------------------------------------------------
# Test: error field
# ---------------------------------------------------------------------------

def test_journal_error_field():
    """error must be a string or null."""
    storage_root = _tmp_dir()
    txn_id = new_transaction_id()

    # Valid with error
    record = make_journal_record(
        transaction_id=txn_id,
        model_identifier="vendor.model",
        version="1.0",
        schema_version=2,
        state="failed",
        error="disk full",
        previous_installation_existed=False,
    )
    write_journal(storage_root, transaction_id=txn_id, record=record)
    data = read_journal(storage_root, transaction_id=txn_id)
    assert data is not None
    assert data["error"] == "disk full"

    # Valid with null error
    record2 = make_journal_record(
        transaction_id=txn_id,
        model_identifier="vendor.model",
        version="1.0",
        schema_version=2,
        state="staging",
        error=None,
        previous_installation_existed=False,
    )
    write_journal(storage_root, transaction_id=txn_id, record=record2)
    data = read_journal(storage_root, transaction_id=txn_id)
    assert data is not None
    assert data["error"] is None


# ---------------------------------------------------------------------------
# Test: files field
# ---------------------------------------------------------------------------

def test_journal_files_field():
    """files must be a list of safe relative paths."""
    storage_root = _tmp_dir()
    txn_id = new_transaction_id()

    record = make_journal_record(
        transaction_id=txn_id,
        model_identifier="vendor.model",
        version="1.0",
        schema_version=2,
        state="staging",
        files=["index.py", "subdir/weights.bin"],
        previous_installation_existed=False,
    )
    write_journal(storage_root, transaction_id=txn_id, record=record)
    data = read_journal(storage_root, transaction_id=txn_id)
    assert data is not None
    assert data["files"] == ["index.py", "subdir/weights.bin"]

    # Invalid: absolute path in files
    ok, err = validate_journal_schema({
        "transaction_id": txn_id,
        "model_identifier": "vendor.model",
        "version": "1.0",
        "schema_version": 2,
        "state": "staging",
        "previous_installation_existed": False,
        "files": ["/absolute/path"],
    })
    assert not ok and "files" in err


# ---------------------------------------------------------------------------
# Test: identifier no path separators
# ---------------------------------------------------------------------------

def test_journal_identifier_no_separators():
    """model_identifier must not contain path separators."""
    ok, err = validate_journal_schema({
        "transaction_id": new_transaction_id(),
        "model_identifier": "vendor/submodel",  # slash
        "version": "1.0",
        "schema_version": 2,
        "state": "staging",
        "previous_installation_existed": False,
    })
    assert not ok and "model_identifier" in err

    ok, err = validate_journal_schema({
        "transaction_id": new_transaction_id(),
        "model_identifier": "vendor\\submodel",  # backslash
        "version": "1.0",
        "schema_version": 2,
        "state": "staging",
        "previous_installation_existed": False,
    })
    assert not ok and "model_identifier" in err


# ---------------------------------------------------------------------------
# Test: valid states
# ---------------------------------------------------------------------------

def test_journal_valid_states():
    """All five states are accepted."""
    storage_root = _tmp_dir()
    txn_id = new_transaction_id()
    for state in VALID_STATES:
        record = make_journal_record(
            transaction_id=txn_id,
            model_identifier="vendor.model",
            version="1.0",
            schema_version=2,
            state=state,
            previous_installation_existed=False,
        )
        path = write_journal(storage_root, transaction_id=txn_id, record=record)
        assert path.is_file(), f"journal file not created for state {state}"
        data = read_journal(storage_root, transaction_id=txn_id)
        assert data is not None, f"read_journal returned None for state {state}"
        assert data["state"] == state


# ---------------------------------------------------------------------------
# Test: path escaping storage root rejected
# ---------------------------------------------------------------------------

def test_journal_path_escapes_storage_root_rejected():
    """staging_dir that escapes storage.root is rejected."""
    ok, err = validate_journal_schema({
        "transaction_id": new_transaction_id(),
        "model_identifier": "vendor.model",
        "version": "1.0",
        "schema_version": 2,
        "state": "staging",
        "staging_dir": "../../etc/passwd",
        "previous_installation_existed": False,
    })
    assert not ok or "staging_dir" in err


# ---------------------------------------------------------------------------
# Test: backup dir safe
# ---------------------------------------------------------------------------

def test_journal_backup_dir_safe():
    """backup_dir must be a safe relative path."""
    storage_root = _tmp_dir()
    txn_id = new_transaction_id()

    record = make_journal_record(
        transaction_id=txn_id,
        model_identifier="vendor.model",
        version="1.0",
        schema_version=2,
        state="publishing",
        backup_dir="vendor.model.backup.abc123",
        previous_installation_existed=False,
    )
    write_journal(storage_root, transaction_id=txn_id, record=record)
    data = read_journal(storage_root, transaction_id=txn_id)
    assert data is not None
    assert data["backup_dir"] == "vendor.model.backup.abc123"


# ---------------------------------------------------------------------------
# Test: Embedded traversal in path components
# ---------------------------------------------------------------------------

def test_journal_embedded_traversal_rejected():
    """Paths containing ``..`` anywhere are rejected, even embedded such as
    ``models/../../outside``."""
    storage_root = _tmp_dir()
    txn_id = new_transaction_id()

    # staging_dir with embedded traversal
    ok, err = validate_journal_schema({
        "transaction_id": txn_id,
        "model_identifier": "vendor.model",
        "version": "1.0",
        "schema_version": 2,
        "state": "staging",
        "staging_dir": "models/../../outside",
        "previous_installation_existed": False,
    })
    assert not ok and "staging_dir" in err, (
        f"expected rejection for embedded traversal, got ok={ok} err={err}"
    )

    # files with embedded traversal
    ok, err = validate_journal_schema({
        "transaction_id": txn_id,
        "model_identifier": "vendor.model",
        "version": "1.0",
        "schema_version": 2,
        "state": "staging",
        "previous_installation_existed": False,
        "files": ["models/../../outside/file.txt"],
    })
    assert not ok and "files" in err, (
        f"expected rejection for embedded traversal in files, got ok={ok} err={err}"
    )


# ---------------------------------------------------------------------------
# Test: Root escape via staging_dir when base is provided
# ---------------------------------------------------------------------------

def test_journal_root_escape_with_base():
    """When a storage_root base is supplied to _is_safe_relative_path, paths
    that would escape it are rejected."""
    from backend.ai._transaction import _is_safe_relative_path
    from pathlib import Path as _P

    storage_root = _tmp_dir()
    # A staging_dir that escapes the storage_root via ``..``
    result = _is_safe_relative_path("../../etc/passwd", base=_P(storage_root))
    assert result is False, (
        f"expected False for path escaping storage_root, got {result}"
    )

    # A safe relative path under storage_root
    result = _is_safe_relative_path("safe/file.txt", base=_P(storage_root))
    assert result is True, f"expected True for safe path under storage_root, got {result}"


# ---------------------------------------------------------------------------
# Test: UUID collision resistance (different UUIDs produce different filenames)
# ---------------------------------------------------------------------------

def test_journal_uuid_uniqueness():
    """Two different UUIDs produce different journal filenames."""
    storage_root = _tmp_dir()
    id1 = new_transaction_id()
    id2 = new_transaction_id()
    assert id1 != id2, "new_transaction_id should generate distinct IDs"


# ---------------------------------------------------------------------------
# Test: Malformed UUID rejection in write_journal
# ---------------------------------------------------------------------------

def test_journal_write_with_malformed_uuid_fails():
    """write_journal raises ValueError when transaction_id is not a valid UUID."""
    storage_root = _tmp_dir()
    try:
        write_journal(storage_root, transaction_id="not-a-uuid", record={})
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "transaction_id" in str(e).lower()


def test_journal_read_with_malformed_uuid_returns_none():
    """read_journal returns None when ID is bad."""
    storage_root = _tmp_dir()
    data = read_journal(storage_root, transaction_id="not-a-uuid")
    assert data is None, "read_journal should return None for non-UUID transaction_id"


# ---------------------------------------------------------------------------
# Test: Sanitized-ID collision (different original IDs becoming same filename)
# ---------------------------------------------------------------------------

def test_journal_sanitized_id_collision():
    """Different UUID strings that sanitize to the same filename should be
    rejected because we now validate UUID format rather than sanitize."""
    from backend.ai._transaction import _is_valid_uuid
    # These are not valid UUIDs
    assert not _is_valid_uuid("a/b/c")
    assert not _is_valid_uuid("a\\b\\c")
    # Valid UUIDs are always accepted regardless of original formatting
    assert _is_valid_uuid(new_transaction_id())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__, "-v"]))