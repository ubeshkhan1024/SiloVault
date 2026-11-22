import pytest
import hashlib
from pathlib import Path
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
