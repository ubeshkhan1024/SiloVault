"""Focused mocked tests for M4.3.1 single-file model installation."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from backend.ai import ModelInstallationService, ModelStorage
from backend.ai.interfaces import ModelDescriptor, ModelRegistry

PAYLOAD = b"synthetic model artifact bytes"
SHA256 = hashlib.sha256(PAYLOAD).hexdigest()
SHA512 = hashlib.sha512(PAYLOAD).hexdigest()


class FakeDownloader:
    """Writes deterministic bytes; never touches the network."""

    def __init__(self, payload=PAYLOAD, error=None, partial=None):
        self.payload = payload
        self.error = error
        self.partial = partial
        self.calls = []

    def download(self, url, destination):
        self.calls.append((url, Path(destination)))
        if self.partial is not None:
            Path(destination).write_bytes(self.partial)
        if self.error is not None:
            raise self.error
        Path(destination).write_bytes(self.payload)


def _descriptor(**overrides):
    values = dict(
        name="Test Model",
        version="1.0",
        modalities=("text",),
        model_id="vendor.model",
        download_url="https://models.example.test/vendor/model.bin",
        checksum=SHA256,
        model_size_bytes=len(PAYLOAD),
    )
    values.update(overrides)
    return ModelDescriptor(**values)


def _setup(tmp_path, downloader, descriptor=None):
    storage = ModelStorage(root=tmp_path / "models")
    registry = ModelRegistry()
    descriptor = _descriptor() if descriptor is None else descriptor
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, downloader)
    return service, descriptor, storage


def _assert_nothing_published(service, descriptor):
    assert service.is_installed(descriptor) is False
    assert not service.artifact_path(descriptor).exists()
    assert not service.manifest_path(descriptor).exists()
    directory = service.artifact_path(descriptor).parent
    assert list(directory.glob(".install-*")) == []


def test_successful_installation_publishes_artifact_and_manifest(tmp_path):
    downloader = FakeDownloader()
    service, descriptor, storage = _setup(tmp_path, downloader)

    result = service.install(descriptor)

    assert result.status == "installed"
    assert result.installed is True
    assert downloader.calls[0][0] == descriptor.download_url
    artifact = service.artifact_path(descriptor)
    assert artifact.read_bytes() == PAYLOAD
    assert artifact.name == "artifact.bin"

    manifest = json.loads(service.manifest_path(descriptor).read_text("utf-8"))
    assert manifest["identifier"] == "vendor.model"
    assert manifest["version"] == "1.0"
    assert manifest["artifact"] == "artifact.bin"
    assert manifest["size"] == len(PAYLOAD)
    assert manifest["checksum"] == f"sha256:{SHA256}"

    assert service.is_installed(descriptor) is True
    assert storage.is_installed(descriptor) is True
    assert descriptor.local_path is None
    assert list(artifact.parent.glob(".install-*")) == []


def test_declared_sha512_checksum_is_supported(tmp_path):
    descriptor = _descriptor(checksum=f"sha512:{SHA512}")
    service, descriptor, _ = _setup(tmp_path, FakeDownloader(), descriptor)

    result = service.install(descriptor)

    assert result.status == "installed"
    manifest = json.loads(service.manifest_path(descriptor).read_text("utf-8"))
    assert manifest["checksum"] == f"sha512:{SHA512}"


def test_checksum_mismatch_publishes_nothing(tmp_path):
    corrupted = b"X" * len(PAYLOAD)
    service, descriptor, _ = _setup(tmp_path, FakeDownloader(payload=corrupted))

    result = service.install(descriptor)

    assert result.status == "failed"
    assert result.installed is False
    assert "checksum mismatch" in result.error
    _assert_nothing_published(service, descriptor)


def test_size_mismatch_publishes_nothing(tmp_path):
    descriptor = _descriptor(model_size_bytes=len(PAYLOAD) + 1)
    service, descriptor, _ = _setup(tmp_path, FakeDownloader(), descriptor)

    result = service.install(descriptor)

    assert result.status == "failed"
    assert "size mismatch" in result.error
    _assert_nothing_published(service, descriptor)


def test_downloader_failure_publishes_nothing(tmp_path):
    service, descriptor, _ = _setup(
        tmp_path, FakeDownloader(error=RuntimeError("network down"))
    )

    result = service.install(descriptor)

    assert result.status == "failed"
    assert "download failed" in result.error
    assert "network down" in result.error
    _assert_nothing_published(service, descriptor)


def test_interrupted_download_is_cleaned_up_and_not_installed(tmp_path):
    service, descriptor, _ = _setup(
        tmp_path,
        FakeDownloader(partial=PAYLOAD[:5], error=RuntimeError("connection reset")),
    )
    directory = service.artifact_path(descriptor).parent

    result = service.install(descriptor)

    assert result.status == "failed"
    assert result.installed is False
    _assert_nothing_published(service, descriptor)
    assert list(directory.iterdir()) == []


def test_keyboard_interrupt_cleans_staging_and_propagates(tmp_path):
    service, descriptor, _ = _setup(
        tmp_path, FakeDownloader(partial=PAYLOAD[:5], error=KeyboardInterrupt())
    )
    directory = service.artifact_path(descriptor).parent

    with pytest.raises(KeyboardInterrupt):
        service.install(descriptor)

    assert not service.manifest_path(descriptor).exists()
    assert not service.artifact_path(descriptor).exists()
    assert list(directory.glob(".install-*")) == []


def test_failed_installation_can_be_retried(tmp_path):
    service, descriptor, storage = _setup(
        tmp_path, FakeDownloader(payload=b"X" * len(PAYLOAD))
    )
    first = service.install(descriptor)
    assert first.status == "failed"

    retry = ModelInstallationService(storage, service._registry, FakeDownloader())
    second = retry.install(descriptor)

    assert second.status == "installed"
    assert retry.artifact_path(descriptor).read_bytes() == PAYLOAD
    assert retry.is_installed(descriptor) is True
    assert list(retry.artifact_path(descriptor).parent.glob(".install-*")) == []


def test_existing_valid_installation_is_not_overwritten(tmp_path):
    service, descriptor, storage = _setup(tmp_path, FakeDownloader())
    assert service.install(descriptor).status == "installed"
    artifact = service.artifact_path(descriptor)
    manifest_before = service.manifest_path(descriptor).read_bytes()

    replacement = FakeDownloader(payload=b"Y" * len(PAYLOAD))
    second = ModelInstallationService(storage, service._registry, replacement)
    result = second.install(descriptor)

    assert result.status == "already-installed"
    assert result.installed is True
    assert replacement.calls == []
    assert artifact.read_bytes() == PAYLOAD
    assert service.manifest_path(descriptor).read_bytes() == manifest_before


@pytest.mark.parametrize(
    "checksum_change",
    [
        # Different valid digest: metadata no longer matches the descriptor.
        {"checksum": "sha256:" + hashlib.sha256(b"other bytes").hexdigest()},
        # Unsupported algorithm recorded in the manifest.
        {"checksum": "md5:" + "0" * 32},
        # Malformed digest.
        {"checksum": "not-a-checksum"},
        # Missing checksum key.
        None,
    ],
)
def test_manifest_checksum_must_match_descriptor(tmp_path, checksum_change):
    service, descriptor, _ = _setup(tmp_path, FakeDownloader())
    assert service.install(descriptor).status == "installed"
    manifest_path = service.manifest_path(descriptor)
    manifest = json.loads(manifest_path.read_text("utf-8"))
    assert service.is_installed(descriptor) is True

    if checksum_change is None:
        manifest.pop("checksum")
    else:
        manifest.update(checksum_change)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    tampered = manifest_path.read_bytes()

    assert service.is_installed(descriptor) is False
    # Status checks must not rewrite the manifest or mutate the descriptor.
    assert manifest_path.read_bytes() == tampered
    assert service.is_installed(descriptor) is False
    assert descriptor.checksum == SHA256
    assert descriptor.local_path is None


def _failing_artifact_replace(service, descriptor, error):
    """Return an os.replace replacement that fails only for the artifact."""
    real_replace = os.replace
    artifact_name = service.artifact_path(descriptor).name

    def replace(source, destination):
        if Path(destination).name == artifact_name:
            raise error
        return real_replace(source, destination)

    return replace


@pytest.mark.parametrize(
    "error",
    [
        PermissionError("artifact is locked by another process"),
        OSError("disk full"),
    ],
)
def test_artifact_publish_failure_fails_closed(tmp_path, monkeypatch, error):
    service, descriptor, _ = _setup(tmp_path, FakeDownloader())
    monkeypatch.setattr(
        "backend.ai.installation.os.replace",
        _failing_artifact_replace(service, descriptor, error),
    )

    result = service.install(descriptor)

    assert result.status == "failed"
    assert result.installed is False
    assert "artifact publish failed" in result.error
    assert service.is_installed(descriptor) is False
    assert not service.artifact_path(descriptor).exists()
    assert not service.manifest_path(descriptor).exists()
    assert list(service.artifact_path(descriptor).parent.glob(".install-*")) == []


def test_retry_after_artifact_publish_failure(tmp_path, monkeypatch):
    service, descriptor, storage = _setup(tmp_path, FakeDownloader())

    with monkeypatch.context() as patch:
        patch.setattr(
            "backend.ai.installation.os.replace",
            _failing_artifact_replace(
                service, descriptor, PermissionError("artifact is locked")
            ),
        )
        failed = service.install(descriptor)

    assert failed.status == "failed"
    assert failed.installed is False
    assert service.is_installed(descriptor) is False

    retry = ModelInstallationService(storage, service._registry, FakeDownloader())
    result = retry.install(descriptor)

    assert result.status == "installed"
    assert result.installed is True
    assert retry.is_installed(descriptor) is True
    assert retry.artifact_path(descriptor).read_bytes() == PAYLOAD


def test_manifest_artifact_must_be_a_regular_file(tmp_path):
    service, descriptor, _ = _setup(tmp_path, FakeDownloader())
    assert service.install(descriptor).status == "installed"
    manifest_path = service.manifest_path(descriptor)
    manifest = json.loads(manifest_path.read_text("utf-8"))
    directory = service.artifact_path(descriptor).parent

    # An empty artifact name resolves to the model directory itself.  Set the
    # recorded size to the directory's size so only the regular-file guard can
    # reject it.
    manifest["artifact"] = ""
    manifest["size"] = directory.stat().st_size
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    tampered = manifest_path.read_bytes()

    assert service.is_installed(descriptor) is False
    assert service.is_installed(descriptor) is False
    assert manifest_path.read_bytes() == tampered
    assert descriptor.local_path is None


def test_unregistered_descriptor_is_rejected_before_download(tmp_path):
    storage = ModelStorage(root=tmp_path / "models")
    downloader = FakeDownloader()
    service = ModelInstallationService(storage, ModelRegistry(), downloader)

    with pytest.raises(ValueError, match="not registered"):
        service.install(_descriptor())

    assert downloader.calls == []
    assert not storage.root.exists()


def test_missing_url_or_checksum_is_rejected_before_download(tmp_path):
    for overrides in ({"download_url": None}, {"checksum": None}):
        downloader = FakeDownloader()
        descriptor = _descriptor(**overrides)
        service, descriptor, _ = _setup(tmp_path, downloader, descriptor)

        with pytest.raises(ValueError):
            service.install(descriptor)

        assert downloader.calls == []
        assert service.is_installed(descriptor) is False


@pytest.mark.parametrize(
    "checksum",
    [
        "md5:" + "0" * 32,
        "sha256:" + "0" * 63,
        "sha256:" + "0" * 62,
    ],
)
def test_unsupported_or_malformed_checksum_is_rejected_before_download(
    tmp_path, checksum
):
    downloader = FakeDownloader()
    descriptor = _descriptor(checksum=checksum)
    service, descriptor, _ = _setup(tmp_path, downloader, descriptor)

    with pytest.raises(ValueError):
        service.install(descriptor)

    assert downloader.calls == []
    assert not service.artifact_path(descriptor).parent.exists()


# ---------------------------------------------------------------------------
# v2 multi-file installation recognition tests
# ---------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _write_v2_installation(
    tmp_path, descriptor, files: dict[str, bytes], entry_point: str | None = None
):
    """Write a v2 manifest and files into the model directory, return service."""
    storage = ModelStorage(root=tmp_path / "models")
    registry = ModelRegistry()
    registry.register(descriptor)
    service = ModelInstallationService(storage, registry, None)

    target_dir = service.manifest_path(descriptor).parent
    target_dir.mkdir(parents=True, exist_ok=True)

    manifest_files = []
    for rel_path, data in files.items():
        file_path = target_dir / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(data)
        manifest_files.append({
            "path": rel_path.replace("\\", "/"),
            "size": len(data),
            "sha256": _sha256(data),
        })

    manifest = {
        "schema_version": 2,
        "identifier": descriptor.identifier,
        "version": descriptor.version,
        "packaging": "directory",
        "installed_at": "2026-01-15T10:30:00Z",
        "entry_point": entry_point or list(files.keys())[0],
        "files": manifest_files,
        "source": {"type": "local", "local_path": str(tmp_path / "source")},
    }
    service.manifest_path(descriptor).write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return service


def test_is_installed_v2_valid_returns_true(tmp_path):
    """Valid v2 manifest with all files present and matching is installed."""
    descriptor = _descriptor(model_id="dir.model")
    files = {"index.py": b"print('hello')", "subdir/weights.bin": b"weights"}
    service = _write_v2_installation(tmp_path, descriptor, files)
    assert service.is_installed(descriptor) is True


def test_is_installed_v2_missing_file_returns_false(tmp_path):
    """v2 manifest listing a file that does not exist is not installed."""
    descriptor = _descriptor(model_id="dir.model")
    files = {"index.py": b"print('hello')", "missing.txt": b"gone"}
    service = _write_v2_installation(tmp_path, descriptor, files)
    # Remove one file after writing the manifest
    (service.manifest_path(descriptor).parent / "missing.txt").unlink()
    assert service.is_installed(descriptor) is False


def test_is_installed_v2_wrong_size_returns_false(tmp_path):
    """v2 manifest with a wrong recorded size is not installed."""
    descriptor = _descriptor(model_id="dir.model")
    files = {"index.py": b"print('hello')"}
    service = _write_v2_installation(tmp_path, descriptor, files)
    # Tamper with the recorded size
    manifest_path = service.manifest_path(descriptor)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["size"] = 99999
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert service.is_installed(descriptor) is False


def test_is_installed_v2_wrong_hash_returns_false(tmp_path):
    """v2 manifest with a wrong SHA-256 is not installed."""
    descriptor = _descriptor(model_id="dir.model")
    files = {"index.py": b"print('hello')"}
    service = _write_v2_installation(tmp_path, descriptor, files)
    # Tamper with the recorded hash
    manifest_path = service.manifest_path(descriptor)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert service.is_installed(descriptor) is False


def test_is_installed_v2_invalid_entry_point_returns_false(tmp_path):
    """v2 manifest whose entry_point is not in the files list is not installed."""
    descriptor = _descriptor(model_id="dir.model")
    files = {"index.py": b"print('hello')"}
    service = _write_v2_installation(tmp_path, descriptor, files, entry_point="missing.py")
    assert service.is_installed(descriptor) is False


def test_is_installed_v2_path_traversal_returns_false(tmp_path):
    """v2 manifest with a ``..`` path component is not installed."""
    descriptor = _descriptor(model_id="dir.model")
    files = {"index.py": b"print('hello')"}
    service = _write_v2_installation(tmp_path, descriptor, files)
    # Tamper with a path to escape the model directory
    manifest_path = service.manifest_path(descriptor)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../../etc/passwd"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert service.is_installed(descriptor) is False


def test_is_installed_v2_symlink_file_returns_false(tmp_path):
    """v2 manifest listing a symlink as a file is not installed."""
    descriptor = _descriptor(model_id="dir.model")
    files = {"index.py": b"print('hello')"}
    service = _write_v2_installation(tmp_path, descriptor, files)
    # Replace the real file with a symlink
    target_dir = service.manifest_path(descriptor).parent
    index_path = target_dir / "index.py"
    index_path.unlink()
    try:
        index_path.symlink_to(tmp_path / "outside_target.py")
    except OSError:
        pytest.skip("Symlinks not supported on this platform/environment")
    assert service.is_installed(descriptor) is False


def test_is_installed_v1_still_recognized(tmp_path):
    """v1 single-file manifest recognition is unchanged."""
    downloader = FakeDownloader()
    service, descriptor, _ = _setup(tmp_path, downloader)
    assert service.install(descriptor).status == "installed"
    assert service.is_installed(descriptor) is True
