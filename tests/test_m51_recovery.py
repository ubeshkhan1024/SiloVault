import pytest
import hashlib
import json
import os
import stat
from pathlib import Path
from unittest.mock import patch
from backend.ai import ModelInstallationService, ModelStorage
from backend.ai.interfaces import ModelDescriptor, ModelRegistry


def _descriptor(**overrides):
    values = dict(
        name="Test Model",
        version="1.0",
        modalities=("text",),
        model_id="vendor.model",
        download_url="https://models.example.test/vendor/model.bin",
        checksum="sha256:" + "0" * 64,
        model_size_bytes=100,
    )
    values.update(overrides)
    return ModelDescriptor(**values)


def test_recover_staging_state(tmp_path):
    """Recover a transaction stuck in staging state."""
    storage_root = tmp_path / "models"
    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    # Create a staging directory with a journal
    staging_rel = ".staging-dir.model-123456"
    staging_dir = storage_root / staging_rel
    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "index.py").write_text("print('hello')")

    txns_dir = storage_root / ".transactions"
    txns_dir.mkdir(parents=True, exist_ok=True)
    journal = {
        "transaction_id": "123e4567-e89b-12d3-a456-426614174000",
        "model_identifier": "dir.model",
        "version": "1.0",
        "schema_version": 2,
        "state": "staging",
        "staging_dir": staging_rel,
        "target_dir": "dir.model",
        "backup_dir": None,
        "previous_installation_existed": False,
    }
    import json
    (txns_dir / "123e4567-e89b-12d3-a456-426614174000.json").write_text(
        json.dumps(journal), encoding="utf-8"
    )

    # Run recovery
    service.recover()

    # Staging should be cleaned up
    assert not staging_dir.exists()
    # Journal should be rolled back
    data = json.loads((txns_dir / "123e4567-e89b-12d3-a456-426614174000.json").read_text())
    assert data["state"] == "rolled_back"


def test_recover_publishing_with_valid_target(tmp_path):
    """Recover a publishing transaction with a valid target."""
    storage_root = tmp_path / "models"
    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    # Create a valid target with manifest
    target_dir = storage_root / "dir.model"
    target_dir.mkdir(parents=True, exist_ok=True)
    content = "print('hello')"
    (target_dir / "index.py").write_text(content)
    
    import json
    manifest = {
        "schema_version": 2,
        "identifier": "dir.model",
        "version": "1.0",
        "packaging": "directory",
        "installed_at": "2023-01-01T00:00:00Z",
        "entry_point": "index.py",
        "files": [
            {
                "path": "index.py",
                "size": len(content.encode("utf-8")),
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()
            }
        ],
        "source": {"type": "local", "local_path": "/tmp/source"}
    }
    (target_dir / "installation.json").write_text(json.dumps(manifest))

    # Create a journal in publishing state
    txns_dir = storage_root / ".transactions"
    txns_dir.mkdir(parents=True, exist_ok=True)
    journal = {
        "transaction_id": "123e4567-e89b-12d3-a456-426614174000",
        "model_identifier": "dir.model",
        "version": "1.0",
        "schema_version": 2,
        "state": "publishing",
        "staging_dir": None,
        "target_dir": "dir.model",
        "backup_dir": None,
        "previous_installation_existed": False,
    }
    import json
    (txns_dir / "123e4567-e89b-12d3-a456-426614174000.json").write_text(
        json.dumps(journal), encoding="utf-8"
    )

    # Run recovery
    service.recover()

    # Journal should be committed
    journal_file = txns_dir / "123e4567-e89b-12d3-a456-426614174000.json"
    assert journal_file.exists()
    data = json.loads(journal_file.read_text(encoding="utf-8"))
    assert data["state"] == "committed"


def test_recover_publishing_with_backup(tmp_path):
    """Recover a publishing transaction with a backup."""
    storage_root = tmp_path / "models"
    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    # Create a previous installation (backup)
    backup_dir = storage_root / "dir.model.backup.abc123"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "index.py").write_text("print('old')")

    # Create a target (empty)
    target_dir = storage_root / "dir.model"
    target_dir.mkdir(parents=True, exist_ok=True)

    # Create a journal in publishing state with backup
    txns_dir = storage_root / ".transactions"
    txns_dir.mkdir(parents=True, exist_ok=True)
    journal = {
        "transaction_id": "123e4567-e89b-12d3-a456-426614174000",
        "model_identifier": "dir.model",
        "version": "1.0",
        "schema_version": 2,
        "state": "publishing",
        "staging_dir": None,
        "target_dir": "dir.model",
        "backup_dir": "dir.model.backup.abc123",
        "previous_installation_existed": True,
    }
    import json
    (txns_dir / "123e4567-e89b-12d3-a456-426614174000.json").write_text(
        json.dumps(journal), encoding="utf-8"
    )

    # Run recovery
    service.recover()

    # Backup should be restored to target
    assert (target_dir / "index.py").read_text() == "print('old')"
    # Journal should be rolled back
    journal_file = txns_dir / "123e4567-e89b-12d3-a456-426614174000.json"
    assert journal_file.exists()
    data = json.loads(journal_file.read_text(encoding="utf-8"))
    assert data["state"] == "rolled_back"


def test_recover_failed_state(tmp_path):
    """Recover a transaction already in failed state (no-op)."""
    storage_root = tmp_path / "models"
    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    # Create a staging directory
    staging_dir = storage_root / ".staging-dir.model-123456"
    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "index.py").write_text("print('hello')")

    # Create a journal in failed state
    txns_dir = storage_root / ".transactions"
    txns_dir.mkdir(parents=True, exist_ok=True)
    journal = {
        "transaction_id": "123e4567-e89b-12d3-a456-426614174000",
        "model_identifier": "dir.model",
        "version": "1.0",
        "schema_version": 2,
        "state": "failed",
        "staging_dir": None,
        "target_dir": "dir.model",
        "backup_dir": None,
        "previous_installation_existed": False,
    }
    import json
    (txns_dir / "123e4567-e89b-12d3-a456-426614174000.json").write_text(
        json.dumps(journal), encoding="utf-8"
    )

    # Run recovery
    service.recover()

    # Staging should remain (failed transactions are preserved)
    assert staging_dir.exists()
    # Journal should remain failed
    journal_file = txns_dir / "123e4567-e89b-12d3-a456-426614174000.json"
    assert journal_file.exists()
    data = json.loads(journal_file.read_text(encoding="utf-8"))
    assert data["state"] == "failed"


def test_recover_rolled_back_state(tmp_path):
    """Recover a transaction already in rolled_back state (no-op)."""
    storage_root = tmp_path / "models"
    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    # Create a staging directory
    staging_dir = storage_root / ".staging-dir.model-123456"
    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "index.py").write_text("print('hello')")

    # Create a journal in rolled_back state
    txns_dir = storage_root / ".transactions"
    txns_dir.mkdir(parents=True, exist_ok=True)
    journal = {
        "transaction_id": "123e4567-e89b-12d3-a456-426614174000",
        "model_identifier": "dir.model",
        "version": "1.0",
        "schema_version": 2,
        "state": "rolled_back",
        "staging_dir": None,
        "target_dir": "dir.model",
        "backup_dir": None,
        "previous_installation_existed": False,
    }
    import json
    (txns_dir / "123e4567-e89b-12d3-a456-426614174000.json").write_text(
        json.dumps(journal), encoding="utf-8"
    )

    # Run recovery
    service.recover()

    # Staging should remain (rolled_back transactions are preserved)
    assert staging_dir.exists()
    # Journal should remain rolled_back
    journal_file = txns_dir / "123e4567-e89b-12d3-a456-426614174000.json"
    assert journal_file.exists()
    data = json.loads(journal_file.read_text(encoding="utf-8"))
    assert data["state"] == "rolled_back"


def test_recover_invalid_journal(tmp_path):
    """Recover with an invalid journal (should be ignored)."""
    storage_root = tmp_path / "models"
    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    # Create an invalid journal (missing required fields)
    txns_dir = storage_root / ".transactions"
    txns_dir.mkdir(parents=True, exist_ok=True)
    journal = {
        "transaction_id": "123e4567-e89b-12d3-a456-426614174000",
        "model_identifier": "dir.model",
        # Missing version, schema_version, state
    }
    import json
    (txns_dir / "123e4567-e89b-12d3-a456-426614174000.json").write_text(
        json.dumps(journal), encoding="utf-8"
    )

    # Run recovery
    service.recover()

    # Invalid journal should be ignored (no crash)
    journal_file = txns_dir / "123e4567-e89b-12d3-a456-426614174000.json"
    assert journal_file.exists()


def test_recover_rollback_failure_preserves_backup(tmp_path):
    """Recover a publishing transaction where rollback move fails."""
    storage_root = tmp_path / "models"
    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    # Create a backup with valid content
    backup_dir = storage_root / "dir.model.backup.abc123"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "index.py").write_text("print('old')")

    # Create an incomplete target
    target_dir = storage_root / "dir.model"
    target_dir.mkdir(parents=True, exist_ok=True)

    # Create a journal in publishing state with backup
    txns_dir = storage_root / ".transactions"
    txns_dir.mkdir(parents=True, exist_ok=True)
    journal = {
        "transaction_id": "123e4567-e89b-12d3-a456-426614174000",
        "model_identifier": "dir.model",
        "version": "1.0",
        "schema_version": 2,
        "state": "publishing",
        "staging_dir": None,
        "target_dir": "dir.model",
        "backup_dir": "dir.model.backup.abc123",
        "previous_installation_existed": True,
    }
    import json
    (txns_dir / "123e4567-e89b-12d3-a456-426614174000.json").write_text(
        json.dumps(journal), encoding="utf-8"
    )

    # Simulate shutil.move failing during rollback
    with patch("backend.ai._recovery.shutil.move", side_effect=OSError("move failed")):
        service.recover()

    # Backup should remain intact after failed rollback
    assert backup_dir.exists()
    assert (backup_dir / "index.py").read_text() == "print('old')"

    # Journal should remain available (not rolled_back since move failed)
    journal_file = txns_dir / "123e4567-e89b-12d3-a456-426614174000.json"
    assert journal_file.exists()
    data = json.loads(journal_file.read_text(encoding="utf-8"))
    assert data["state"] == "publishing"

    # Recovery should be retryable - running again should succeed (move backup to target)
    service.recover()
    # After successful retry, backup is moved to target (no longer exists as backup)
    assert not backup_dir.exists()
    # Target should have the restored content
    assert (target_dir / "index.py").read_text() == "print('old')"
    # Journal should now be rolled_back
    data = json.loads(journal_file.read_text(encoding="utf-8"))
    assert data["state"] == "rolled_back"


def test_recover_journal_commit_failure_remains_recoverable(tmp_path):
    """Recover a publishing transaction where journal commit write fails."""
    storage_root = tmp_path / "models"
    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    # Create a valid target with manifest
    target_dir = storage_root / "dir.model"
    target_dir.mkdir(parents=True, exist_ok=True)
    content = "print('hello')"
    (target_dir / "index.py").write_text(content)

    import json
    manifest = {
        "schema_version": 2,
        "identifier": "dir.model",
        "version": "1.0",
        "packaging": "directory",
        "installed_at": "2023-01-01T00:00:00Z",
        "entry_point": "index.py",
        "files": [
            {
                "path": "index.py",
                "size": len(content.encode("utf-8")),
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()
            }
        ],
        "source": {"type": "local", "local_path": "/tmp/source"}
    }
    (target_dir / "installation.json").write_text(json.dumps(manifest))

    # Create a journal in publishing state
    txns_dir = storage_root / ".transactions"
    txns_dir.mkdir(parents=True, exist_ok=True)
    journal = {
        "transaction_id": "123e4567-e89b-12d3-a456-426614174000",
        "model_identifier": "dir.model",
        "version": "1.0",
        "schema_version": 2,
        "state": "publishing",
        "staging_dir": None,
        "target_dir": "dir.model",
        "backup_dir": None,
        "previous_installation_existed": False,
    }
    (txns_dir / "123e4567-e89b-12d3-a456-426614174000.json").write_text(
        json.dumps(journal), encoding="utf-8"
    )

    # Simulate write_journal failing when committing
    with patch("backend.ai._recovery.write_journal", side_effect=OSError("write failed")):
        service.recover()

    # Journal should remain in publishing state (not committed)
    journal_file = txns_dir / "123e4567-e89b-12d3-a456-426614174000.json"
    assert journal_file.exists()
    data = json.loads(journal_file.read_text(encoding="utf-8"))
    assert data["state"] == "publishing"

    # Target should still exist
    assert target_dir.exists()

    # Subsequent recovery attempt should succeed (write_journal works without mock)
    service.recover()
    data = json.loads(journal_file.read_text(encoding="utf-8"))
    assert data["state"] == "committed"


def test_recover_corrupted_journal(tmp_path):
    """Recover with corrupted/malformed journal files (should not crash)."""
    storage_root = tmp_path / "models"
    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    txns_dir = storage_root / ".transactions"
    txns_dir.mkdir(parents=True, exist_ok=True)

    # Write binary garbage to a journal file
    (txns_dir / "corrupted-binary.json").write_bytes(b"\x00\x01\x02\x03\xff\xfe")

    # Write malformed JSON
    (txns_dir / "malformed-json.json").write_text("{invalid json content", encoding="utf-8")

    # Write a valid journal alongside corrupted ones
    valid_journal = {
        "transaction_id": "123e4567-e89b-12d3-a456-426614174000",
        "model_identifier": "dir.model",
        "version": "1.0",
        "schema_version": 2,
        "state": "staging",
        "staging_dir": ".staging-dir.model-123456",
        "target_dir": "dir.model",
        "backup_dir": None,
        "previous_installation_existed": False,
    }
    (txns_dir / "123e4567-e89b-12d3-a456-426614174000.json").write_text(
        json.dumps(valid_journal), encoding="utf-8"
    )

    # Create the staging directory referenced by the valid journal
    staging_dir = storage_root / ".staging-dir.model-123456"
    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "index.py").write_text("print('hello')")

    # Run recovery - should not crash on corrupted journals
    service.recover()

    # Corrupted journal files should still exist (not deleted)
    assert (txns_dir / "corrupted-binary.json").exists()
    assert (txns_dir / "malformed-json.json").exists()

    # Valid journal should have been processed (staging -> rolled_back)
    data = json.loads((txns_dir / "123e4567-e89b-12d3-a456-426614174000.json").read_text())
    assert data["state"] == "rolled_back"

    # Staging directory should be cleaned up
    assert not staging_dir.exists()


def test_recover_staging_with_existing_backup_restores_backup(tmp_path):
    """Recover a staging transaction that has an existing backup."""
    storage_root = tmp_path / "models"
    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    # Create a backup with valid content
    backup_dir = storage_root / "dir.model.backup.abc123"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "index.py").write_text("print('old')")

    # Create an incomplete target (missing manifest)
    target_dir = storage_root / "dir.model"
    target_dir.mkdir(parents=True, exist_ok=True)

    # Create staging directory
    staging_rel = ".staging-dir.model-123456"
    staging_dir = storage_root / staging_rel
    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "index.py").write_text("print('new')")

    # Create a journal in staging state with backup
    txns_dir = storage_root / ".transactions"
    txns_dir.mkdir(parents=True, exist_ok=True)
    journal = {
        "transaction_id": "123e4567-e89b-12d3-a456-426614174000",
        "model_identifier": "dir.model",
        "version": "1.0",
        "schema_version": 2,
        "state": "staging",
        "staging_dir": staging_rel,
        "target_dir": "dir.model",
        "backup_dir": "dir.model.backup.abc123",
        "previous_installation_existed": True,
    }
    import json
    (txns_dir / "123e4567-e89b-12d3-a456-426614174000.json").write_text(
        json.dumps(journal), encoding="utf-8"
    )

    # Run recovery
    service.recover()

    # Staging should be cleaned up
    assert not staging_dir.exists()

    # Backup should be restored to target (target was incomplete - no manifest)
    assert (target_dir / "index.py").read_text() == "print('old')"

    # Journal should be rolled back
    journal_file = txns_dir / "123e4567-e89b-12d3-a456-426614174000.json"
    assert journal_file.exists()
    data = json.loads(journal_file.read_text(encoding="utf-8"))
    assert data["state"] == "rolled_back"

    # Recovery should be idempotent - running again should not change anything
    service.recover()
    assert (target_dir / "index.py").read_text() == "print('old')"
    data = json.loads(journal_file.read_text(encoding="utf-8"))
    assert data["state"] == "rolled_back"


def test_recover_rejects_symlink_in_target(tmp_path):
    """Recover a publishing transaction with a symlink in the target (should be rejected)."""
    storage_root = tmp_path / "models"
    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    # Skip if symlinks cannot be created (e.g., Windows without developer mode)
    if not _can_create_symlinks(tmp_path):
        pytest.skip("Cannot create symlinks in this environment")

    # Create a target with a symlink instead of a regular file
    target_dir = storage_root / "dir.model"
    target_dir.mkdir(parents=True, exist_ok=True)
    real_file = storage_root / "real-file.txt"
    real_file.write_text("real content")
    (target_dir / "index.py").symlink_to(real_file)

    import json
    manifest = {
        "schema_version": 2,
        "identifier": "dir.model",
        "version": "1.0",
        "packaging": "directory",
        "installed_at": "2023-01-01T00:00:00Z",
        "entry_point": "index.py",
        "files": [
            {
                "path": "index.py",
                "size": len("real content"),
                "sha256": hashlib.sha256(b"real content").hexdigest()
            }
        ],
        "source": {"type": "local", "local_path": "/tmp/source"}
    }
    (target_dir / "installation.json").write_text(json.dumps(manifest))

    # Create a journal in publishing state
    txns_dir = storage_root / ".transactions"
    txns_dir.mkdir(parents=True, exist_ok=True)
    journal = {
        "transaction_id": "123e4567-e89b-12d3-a456-426614174000",
        "model_identifier": "dir.model",
        "version": "1.0",
        "schema_version": 2,
        "state": "publishing",
        "staging_dir": None,
        "target_dir": "dir.model",
        "backup_dir": None,
        "previous_installation_existed": False,
    }
    (txns_dir / "123e4567-e89b-12d3-a456-426614174000.json").write_text(
        json.dumps(journal), encoding="utf-8"
    )

    # Run recovery
    service.recover()

    # Journal should NOT be committed (symlink rejected)
    journal_file = txns_dir / "123e4567-e89b-12d3-a456-426614174000.json"
    assert journal_file.exists()
    data = json.loads(journal_file.read_text(encoding="utf-8"))
    assert data["state"] != "committed"


def _can_create_symlinks(base_path: Path) -> bool:
    """Check if symlinks can be created in the test environment."""
    try:
        test_link = base_path / ".symlink-test"
        test_link.symlink_to("test")
        test_link.unlink()
        return True
    except (OSError, NotImplementedError):
        return False
