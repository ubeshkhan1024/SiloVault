import pytest
import stat
import shutil
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch
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


def test_install_from_directory_success(tmp_path):
    """Successful nested-directory import."""
    storage_root = tmp_path / "models"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "index.py").write_text("print('hello')")
    (source_dir / "subdir").mkdir()
    (source_dir / "subdir" / "weights.bin").write_bytes(b"weights")

    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    result = service.install_from_directory(descriptor, source_dir)
    assert result.status == "installed", f"Failed with error: {result.error}"
    assert (result.artifact_path / "index.py").exists()
    assert (result.artifact_path / "subdir" / "weights.bin").exists()
    assert (result.artifact_path / "installation.json").exists()

    # Check source preservation
    assert source_dir.exists()
    assert (source_dir / "index.py").exists()


def test_install_from_directory_rejects_non_existent(tmp_path):
    """Reject non-existent source directory."""
    storage = ModelStorage(root=tmp_path / "models")
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    result = service.install_from_directory(descriptor, tmp_path / "nonexistent")
    assert result.status == "failed"
    assert "Cannot stat" in result.error or "does not exist" in result.error


def test_install_from_directory_rejects_symlink_to_file(tmp_path):
    """Reject symlink to a file outside the source directory."""
    storage_root = tmp_path / "models"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    target_file = tmp_path / "outside" / "target.py"
    target_file.parent.mkdir()
    target_file.write_text("print('target')")

    try:
        (source_dir / "link.py").symlink_to(target_file)
    except OSError:
        pytest.skip("Symlinks not supported on this platform/environment")

    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    result = service.install_from_directory(descriptor, source_dir)
    assert result.status == "failed"
    assert "Symbolic link" in result.error or "symlink" in result.error.lower()

    # Verify no partial installation was published
    assert not (storage_root / "dir.model").exists()


def test_install_from_directory_commit_journal_failure_preserves_backup(tmp_path, monkeypatch):
    """If committed journal write fails, backup is preserved and failure is reported.

    The installation is valid on disk but the journal is not committed.
    The backup must NOT be deleted, and the result must not report success.
    """
    storage_root = tmp_path / "models"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "index.py").write_text("print('hello')")

    # Pre-create a previous installation so a backup directory will be created
    target_dir = storage_root / "dir.model"
    target_dir.mkdir(parents=True)
    (target_dir / "index.py").write_text("print('old')")

    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    call_states = []

    def failing_write_journal(sr, *, transaction_id, record):
        call_states.append(record.get("state"))
        if record.get("state") == "committed":
            raise OSError("disk full on commit")
        from backend.ai._transaction import write_journal as real_write_journal

        return real_write_journal(sr, transaction_id=transaction_id, record=record)

    monkeypatch.setattr("backend.ai.installation.write_journal", failing_write_journal)

    result = service.install_from_directory(descriptor, source_dir)

    assert result.status == "failed"
    assert "journal commit failed" in result.error

    # Backup must be preserved
    backup_dirs = list(storage_root.glob("dir.model.backup.*"))
    assert len(backup_dirs) == 1, f"expected 1 backup dir, got {len(backup_dirs)}"
    assert backup_dirs[0].exists()
    assert (backup_dirs[0] / "index.py").exists()

    # Target dir exists (installation on disk) but not committed
    assert target_dir.exists()
    assert (target_dir / "installation.json").exists()

    # Journal should not be committed on disk — the committed write was intercepted
    txns_dir = storage_root / ".transactions"
    journal_files = list(txns_dir.glob("*.json"))
    assert len(journal_files) >= 1, "no journal files found"
    import json

    for jf in journal_files:
        data = json.loads(jf.read_text(encoding="utf-8"))
        assert data["state"] != "committed", f"journal {jf.name} should not be committed"


def test_install_from_directory_rollback_restore_fails_preserves_backup(
    tmp_path, monkeypatch
):
    """If rollback restore fails after publication failure, backup is preserved.

    The previous installation remains recoverable in the backup directory.
    """
    storage_root = tmp_path / "models"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "index.py").write_text("print('hello')")

    # Pre-create a previous installation so a backup directory will be created
    target_dir = storage_root / "dir.model"
    target_dir.mkdir(parents=True)
    (target_dir / "index.py").write_text("print('old')")

    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    call_count = [0]
    real_move = shutil.move

    def failing_move(src, dst):
        call_count[0] += 1
        # Call 1: target -> backup (publication backup) - succeeds
        # Call 2: staging -> target (publication move) - fails
        # Call 3: backup -> target (rollback restore) - fails
        if call_count[0] == 2:
            raise OSError("publication move failed")
        if call_count[0] == 3:
            raise OSError("rollback restore failed")
        return real_move(src, dst)

    monkeypatch.setattr("backend.ai.installation.shutil.move", failing_move)

    result = service.install_from_directory(descriptor, source_dir)

    assert result.status == "failed"

    # Backup must be preserved
    backup_dirs = list(storage_root.glob("dir.model.backup.*"))
    assert len(backup_dirs) == 1, f"expected 1 backup dir, got {len(backup_dirs)}"
    assert backup_dirs[0].exists()

    # Previous installation is recoverable from backup
    assert (backup_dirs[0] / "index.py").exists()
    assert (backup_dirs[0] / "index.py").read_text() == "print('old')"


def test_install_from_directory_rejects_symlink_to_dir(tmp_path):
    """Reject symlink to a directory outside the source directory."""
    storage_root = tmp_path / "models"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    target_dir = tmp_path / "outside" / "target_dir"
    target_dir.mkdir(parents=True)
    (target_dir / "file.txt").write_text("data")

    try:
        (source_dir / "link_dir").symlink_to(target_dir)
    except OSError:
        pytest.skip("Symlinks not supported on this platform/environment")

    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    result = service.install_from_directory(descriptor, source_dir)
    assert result.status == "failed"
    assert "Symbolic link" in result.error or "symlink" in result.error.lower()

    # Verify no partial installation was published
    assert not (storage_root / "dir.model").exists()


def test_install_from_directory_source_root_symlink_rejected(tmp_path):
    """Reject symbolic link as the source root itself."""
    storage_root = tmp_path / "models"
    # Create a symlink to a real directory as the source
    real_source = tmp_path / "real_source"
    real_source.mkdir()
    (source_dir_symlink := tmp_path / "source_symlink").symlink_to(real_source)

    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    # Should reject because source root is a symlink
    result = service.install_from_directory(descriptor, tmp_path / "source_symlink")
    assert result.status == "failed"
    assert "Symbolic link" in result.error or "symlink" in result.error.lower()

    # Verify no partial installation was published
    assert not (storage_root / "dir.model").exists()


def test_install_from_directory_rejects_reparse_point(tmp_path):
    """Reject entry with FILE_ATTRIBUTE_REPARSE_POINT."""
    storage_root = tmp_path / "models"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "normal.py").write_text("print('hello')")

    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    # Mock the lstat result to simulate a reparse point for one specific file
    original_lstat = Path.lstat

    def mock_lstat(self):
        if self.name == "normal.py":
            # Create a mock stat result with FILE_ATTRIBUTE_REPARSE_POINT (0x400)
            mock_st = MagicMock()
            mock_st.st_file_attributes = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
            mock_st.st_mode = stat.S_IFREG | 0o644  # Regular file mode
            return mock_st
        return original_lstat(self)

    with patch.object(Path, 'lstat', mock_lstat):
        result = service.install_from_directory(descriptor, source_dir)
        assert result.status == "failed"
        assert "reparse" in result.error.lower()

        # Verify no partial installation was published
        assert not (storage_root / "dir.model").exists()


def test_install_from_directory_rejects_non_regular_entry(tmp_path):
    """Reject non-regular filesystem entries (e.g., FIFO, socket, device)."""
    storage_root = tmp_path / "models"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "normal.py").write_text("print('hello')")

    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    # Mock the lstat result to simulate a non-regular file (FIFO)
    original_lstat = Path.lstat

    def mock_lstat(self):
        if self.name == "normal.py":
            # Create a mock stat result with FIFO mode
            mock_st = MagicMock()
            mock_st.st_mode = stat.S_IFIFO | 0o644  # FIFO mode
            # No st_file_attributes on non-Windows
            if hasattr(mock_st, 'st_file_attributes'):
                mock_st.st_file_attributes = 0
            return mock_st
        return original_lstat(self)

    with patch.object(Path, 'lstat', mock_lstat):
        result = service.install_from_directory(descriptor, source_dir)
        assert result.status == "failed"
        assert "non-regular" in result.error.lower()

        # Verify no partial installation was published
        assert not (storage_root / "dir.model").exists()


def test_install_from_directory_rejects_junction_detection_failure(tmp_path):
    """Reject when Windows junction detection raises OSError (fail closed)."""
    from backend.ai import installation
    storage_root = tmp_path / "models"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "normal.py").write_text("print('hello')")

    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    # Mock Path.is_junction to raise OSError
    original_is_junction = getattr(Path, 'is_junction', None)

    def mock_is_junction(self):
        raise OSError("Permission denied")

    if original_is_junction:
        with patch.object(Path, 'is_junction', mock_is_junction):
            result = service.install_from_directory(descriptor, source_dir)
            assert result.status == "failed"
            assert "junction detection failed" in result.error.lower() or "fail closed" in result.error.lower()

            # Verify no partial installation was published
            assert not (storage_root / "dir.model").exists()
    else:
        pytest.skip("is_junction not available on this Python version")


def test_install_from_directory_rejects_invalid_nested_directory(tmp_path):
    """Reject an invalid nested directory before any file is copied."""
    storage_root = tmp_path / "models"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "index.py").write_text("print('hello')")
    # Create a symlinked directory inside the source
    target_dir = tmp_path / "outside" / "target_dir"
    target_dir.mkdir(parents=True)
    try:
        (source_dir / "bad_dir").symlink_to(target_dir)
    except OSError:
        pytest.skip("Symlinks not supported on this platform/environment for directories")

    storage = ModelStorage(root=storage_root)
    registry = ModelRegistry()
    descriptor = _descriptor(model_id="dir.model")
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    # Should reject because nested directory is a symlink
    result = service.install_from_directory(descriptor, source_dir)
    assert result.status == "failed"
    assert "Symbolic link" in result.error or "symlink" in result.error.lower()

    # Verify no partial installation was published
    assert not (storage_root / "dir.model").exists()