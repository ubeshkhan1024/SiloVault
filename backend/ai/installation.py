"""Local, single-file model installation for SiloVault M4.3.1.

:class:`ModelInstallationService` coordinates one model artifact from an
injected downloader into the directory owned by :class:`ModelStorage`.  It
never performs network requests itself, never mutates a ``ModelDescriptor``,
and never loads or runs a model.

Installation is deliberately transactional:

1. the artifact is downloaded into a temporary staging file inside the model's
   storage directory,
2. its exact size (when declared) and checksum are verified,
3. only then is the artifact published atomically, followed by a completion
   manifest.

A missing manifest always means "not installed", so failures or interruptions
cannot be mistaken for a successful installation.
"""

from __future__ import annotations
from urllib.parse import urlparse
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Literal, Optional, Protocol, Tuple

from backend.ai.interfaces import ModelDescriptor, ModelRegistry
from backend.ai._manifest_validation import _validate_v1_manifest, _validate_v2_manifest
from backend.ai._transaction import make_journal_record, write_journal, new_transaction_id, _is_safe_relative_path
from backend.ai.storage import ModelStorage

MANIFEST_NAME = "installation.json"

# Versioned manifest support
_MANIFEST_V1_SCHEMA = {
    "identifier": True,
    "version": True,
    "artifact": True,
    "size": True,
    "checksum": True,
    "source_url": True,
}
_MANIFEST_V2_SCHEMA = {
    "schema_version": True,
    "identifier": True,
    "version": True,
    "packaging": True,
    "installed_at": True,
    "entry_point": True,
    "files": {
        "type": "array",
        "items": {
            "type": "object",
            "required": ["path", "size", "sha256"],
            "properties": {
                "path": {"type": "string", "pattern": "^[^/\\]+$"},
                "size": {"type": "integer", "minimum": 0},
                "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "description": {"type": "string"}
            }
        }
    },
    "source": {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "url": {"type": "string"},
        "local_path": {"type": "string"}
    }
}
}

from datetime import datetime, timezone
_SUPPORTED_CHECKSUMS = {"sha256": 64, "sha512": 128}

#: Supported checksum algorithms and their hexadecimal digest lengths.
_SUPPORTED_CHECKSUMS = {"sha256": 64, "sha512": 128}

InstallationStatus = Literal["installed", "already-installed", "failed"]


class ModelDownloader(Protocol):
    """Injected artifact downloader.

    Implementations write the bytes for ``url`` to ``destination``.  The
    service owns the destination path (a staging file) and the surrounding
    lifecycle; the downloader must not perform installation or verification.
    """

    def download(self, url: str, destination: Path) -> None: ...


@dataclass(frozen=True)
class InstallationResult:
    """Outcome of an installation attempt.

    ``status`` is ``"installed"`` when this call installed the artifact,
    ``"already-installed"`` when a valid installation was left untouched, and
    ``"failed"`` when nothing was published.  ``error`` is only set for
    failures.
    """

    status: InstallationStatus
    artifact_path: Optional[Path] = None
    manifest_path: Optional[Path] = None
    error: Optional[str] = None

    @property
    def installed(self) -> bool:
        """Return whether the model is installed after this attempt."""
        return self.status in ("installed", "already-installed")


class ModelInstallationService:
    """Install single-file model artifacts into local model storage.

    Parameters
    ----------
    storage:
        Storage layout that owns the model directories.
    registry:
        Catalogue used to confirm the descriptor is registered.
    downloader:
        Injected downloader implementation.  The service never constructs or
        calls a network client of its own.
    """

    def __init__(
        self,
        storage: ModelStorage,
        registry: ModelRegistry,
        downloader: ModelDownloader,
    ) -> None:
        self._storage = storage
        self._registry = registry
        self._downloader = downloader

    # -- public paths -------------------------------------------------------

    def artifact_path(self, descriptor: ModelDescriptor) -> Path:
        """Return the artifact path for *descriptor* (not created)."""
        directory = self._storage.model_directory(descriptor.identifier)
        return directory / _artifact_name(descriptor.download_url)

    def manifest_path(self, descriptor: ModelDescriptor) -> Path:
        """Return the completion manifest path for *descriptor*."""
        directory = self._storage.model_directory(descriptor.identifier)
        return directory / MANIFEST_NAME

    # -- state --------------------------------------------------------------

    def is_installed(self, descriptor: ModelDescriptor) -> bool:
        """Return whether a valid installation exists for *descriptor*.

        A descriptor with an existing ``local_path`` keeps that location.  For
        service-managed installations the completion manifest must exist, name
        this model and version, and point at an artifact whose size matches.

        Both v1 (single-file) and v2 (multi-file) manifests are recognized.
        Journal transaction state is never consulted: a valid manifest means
        the files are present and verified on disk, regardless of whether the
        publication transaction has been committed.
        """
        if descriptor.local_path is not None and Path(descriptor.local_path).exists():
            return True
        manifest_path = self.manifest_path(descriptor)
        manifest = _read_manifest(manifest_path)
        if manifest is None:
            return False
        if manifest.get("identifier") != descriptor.identifier:
            return False
        if manifest.get("version") != descriptor.version:
            return False

        schema_version = manifest.get("schema_version")
        if schema_version == 2 or "files" in manifest:
            return self._is_installed_v2(descriptor, manifest, manifest_path)
        return self._is_installed_v1(descriptor, manifest, manifest_path)

    def _is_installed_v1(
        self, descriptor: ModelDescriptor, manifest: dict, manifest_path: Path
    ) -> bool:
        """v1 single-file installation recognition (existing behavior)."""
        artifact_name = manifest.get("artifact")
        if not isinstance(artifact_name, str) or Path(artifact_name).name != artifact_name:
            return False
        artifact = manifest_path.parent / artifact_name
        if not artifact.is_file():
            return False
        try:
            size = artifact.stat().st_size
        except OSError:
            return False
        if manifest.get("size") != size:
            return False
        if descriptor.model_size_bytes is not None and size != descriptor.model_size_bytes:
            return False

        # The manifest must record the same verified checksum as the
        # descriptor.  Malformed, unsupported, or missing metadata means the
        # installation cannot be trusted.
        if not descriptor.checksum:
            return False
        recorded_checksum = manifest.get("checksum")
        if not isinstance(recorded_checksum, str):
            return False
        try:
            expected_checksum = _normalize_checksum(descriptor.checksum)
            actual_checksum = _normalize_checksum(recorded_checksum)
        except ValueError:
            return False
        if actual_checksum != expected_checksum:
            return False

        return True

    def _is_installed_v2(
        self, descriptor: ModelDescriptor, manifest: dict, manifest_path: Path
    ) -> bool:
        """v2 multi-file installation recognition.

        Verifies that every listed file exists as a regular file (never a
        symlink or junction), has the exact recorded size and SHA-256 hash,
        and that no path escapes the model directory.  The entry point, if
        present, must refer to a valid listed file.
        """
        target_dir = manifest_path.parent
        target_dir_resolved = target_dir.resolve()

        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            return False

        seen_paths: set[str] = set()
        for file_info in files:
            if not isinstance(file_info, dict):
                return False

            path = file_info.get("path")
            if not isinstance(path, str) or not path:
                return False

            # Reject absolute paths, Windows drive letters, backslashes.
            if Path(path).is_absolute():
                return False
            if len(path) >= 2 and path[1] == ":":
                return False
            if "\\" in path:
                return False

            # Reject traversal components.
            parts = Path(path).parts
            if ".." in parts:
                return False

            # Resolve under target_dir and verify containment.
            file_path = (target_dir / path).resolve()
            try:
                file_path.relative_to(target_dir_resolved)
            except ValueError:
                return False

            # Must be a regular file, never a symlink/junction (lstat does not
            # follow links).
            try:
                st = file_path.lstat()
            except OSError:
                return False
            if not stat.S_ISREG(st.st_mode):
                return False

            # Exact size match.
            size = file_info.get("size")
            if type(size) is not int or size < 0:
                return False
            if st.st_size != size:
                return False

            # SHA-256 hash match.
            sha256 = file_info.get("sha256")
            if not isinstance(sha256, str) or len(sha256) != 64:
                return False
            if not re.fullmatch(r"[0-9a-f]+", sha256):
                return False
            actual_sha256 = _hash_file(file_path, "sha256")
            if actual_sha256 != sha256:
                return False

            # Duplicate path check.
            norm_path = path.replace("\\", "/")
            if norm_path in seen_paths:
                return False
            seen_paths.add(norm_path)

        # Entry point, if present, must refer to a valid listed file.
        entry_point = manifest.get("entry_point")
        if entry_point is not None:
            if not isinstance(entry_point, str) or not entry_point:
                return False
            entry_norm = entry_point.replace("\\", "/").lower()
            if not any(
                f.get("path", "").replace("\\", "/").lower() == entry_norm
                for f in files
                if isinstance(f, dict)
            ):
                return False

        return True

    # -- installation -------------------------------------------------------

    def install(self, descriptor: ModelDescriptor) -> InstallationResult:
        """Download, verify, and publish a single-file model artifact.

        Configuration problems (unregistered descriptor, missing URL/checksum,
        unsupported algorithm) raise ``ValueError`` before any download.
        Download and verification failures return a failed result and never
        publish an artifact or manifest.
        """
        catalogued = self._require_registered(descriptor)

        if self.is_installed(catalogued):
            return InstallationResult(
                status="already-installed",
                artifact_path=self.artifact_path(catalogued),
                manifest_path=self.manifest_path(catalogued),
            )

        url = catalogued.download_url
        checksum = catalogued.checksum
        if not url:
            raise ValueError("A download_url is required to install a model")
        if not checksum:
            raise ValueError("A checksum is required to install a model")
        algorithm, expected_digest = _normalize_checksum(checksum)

        directory = self._storage.prepare_model_directory(catalogued.identifier)
        artifact = directory / _artifact_name(url)
        manifest_path = directory / MANIFEST_NAME
        staging = directory / f".install-{uuid.uuid4().hex}.part"

        try:
            self._downloader.download(url, staging)
        except Exception as exc:
            _remove_file(staging)
            return InstallationResult(
                status="failed", error=f"download failed: {exc}"
            )
        except BaseException:
            # Interruptions are cleaned up and propagate; never reported as
            # installed because no manifest is written.
            _remove_file(staging)
            raise

        try:
            try:
                actual_size = staging.stat().st_size
            except OSError as exc:
                return InstallationResult(
                    status="failed", error=f"download failed: {exc}"
                )
            if not staging.is_file():
                return InstallationResult(
                    status="failed", error="download failed: no staging file"
                )
            if (
                catalogued.model_size_bytes is not None
                and actual_size != catalogued.model_size_bytes
            ):
                return InstallationResult(
                    status="failed",
                    error=(
                        "size mismatch: expected "
                        f"{catalogued.model_size_bytes} bytes, got {actual_size}"
                    ),
                )
            actual_digest = _hash_file(staging, algorithm)
            if actual_digest != expected_digest:
                return InstallationResult(
                    status="failed", error="checksum mismatch"
                )

            # Verification passed: publish the artifact, then the manifest.
            try:
                os.replace(staging, artifact)
            except OSError as exc:
                # e.g. Windows PermissionError when the destination is locked.
                # The staging file is removed by the outer ``finally``.
                return InstallationResult(
                    status="failed",
                    error=f"artifact publish failed: {exc}",
                )
            try:
                _write_manifest(
                    manifest_path,
                    {
                        "identifier": catalogued.identifier,
                        "version": catalogued.version,
                        "artifact": artifact.name,
                        "size": actual_size,
                        "checksum": f"{algorithm}:{expected_digest}",
                        "source_url": url,
                    },
                )
            except OSError as exc:
                return InstallationResult(
                    status="failed", error=f"manifest write failed: {exc}"
                )
            return InstallationResult(
                status="installed",
                artifact_path=artifact,
                manifest_path=manifest_path,
            )
        finally:
            # A published staging file no longer exists; this only removes
            # leftovers from failures.
            _remove_file(staging)

    # -- validation ---------------------------------------------------------

    def _require_registered(self, descriptor: ModelDescriptor) -> ModelDescriptor:
        if not isinstance(descriptor, ModelDescriptor):
            raise ValueError("descriptor must be a ModelDescriptor instance")
        try:
            catalogued = self._registry.get(descriptor.identifier)
        except KeyError:
            raise ValueError(
                f"Model '{descriptor.identifier}' is not registered"
            ) from None
        if catalogued != descriptor:
            raise ValueError(
                "descriptor does not match the registered model metadata"
            )
        return catalogued

    def install_from_directory(self, descriptor: ModelDescriptor, source_dir: Path) -> InstallationResult:
        """Install a multi-file model from a local directory with journal-backed publication.

        Publication flow:
        1. Validate source tree (unchanged).
        2. Create journal with state ``staging`` before any filesystem changes.
        3. Copy files to staging, compute sizes and SHA-256 hashes.
        4. Build and validate v2 manifest.
        5. Transition journal to ``publishing``.
        6. If previous installation exists, move target to backup directory.
        7. Move staging to target.
        8. Write manifest to target.
        9. Verify all files exist with correct size and SHA-256.
        10. On success: mark journal ``committed``, remove backup.
        11. On failure: attempt rollback from backup; preserve both dirs if rollback fails.
        """
        return self._do_install_from_directory(descriptor, source_dir)

    def recover(self) -> None:
        """Run startup recovery for all incomplete transactions."""
        from backend.ai._recovery import recover_all
        recover_all(self._storage.root)

    def _do_install_from_directory(self, descriptor: ModelDescriptor, source_dir: Path) -> InstallationResult:
        catalogued = self._require_registered(descriptor)


        # Validate the source root itself using lstat (doesn't follow links)
        root_error = _validate_filesystem_entry(source_dir, source_dir)
        if root_error:
            return InstallationResult(status="failed", error=root_error)
        if not source_dir.is_dir():
            return InstallationResult(status="failed", error=f"Source is not a directory: {source_dir}")

        # Validate the complete source tree before copying any files
        files, dirs, error = _collect_and_validate_source_tree(source_dir)
        if error:
            return InstallationResult(status="failed", error=error)

        # Determine target directory and whether a previous installation existed
        target_dir_path = self._storage.model_directory(catalogued.identifier)
        previous_installation_existed = target_dir_path.exists()
        target_dir = self._storage.prepare_model_directory(catalogued.identifier)

        storage_root = self._storage.root

        # Generate transaction ID and compute relative paths for the journal
        transaction_id = new_transaction_id()
        staging_rel = f".staging-{catalogued.identifier}-{uuid.uuid4().hex}"
        target_rel = str(target_dir.relative_to(storage_root))

        # Validate transaction paths against storage root before filesystem operations
        if not _is_safe_relative_path(staging_rel, base=storage_root):
            return InstallationResult(
                status="failed", error=f"staging path escapes storage root: {staging_rel}"
            )
        if not _is_safe_relative_path(target_rel, base=storage_root):
            return InstallationResult(
                status="failed", error=f"target path escapes storage root: {target_rel}"
            )

        # Create journal with state "staging" before any filesystem changes
        journal_record = make_journal_record(
            transaction_id=transaction_id,
            model_identifier=catalogued.identifier,
            version=catalogued.version,
            schema_version=2,
            state="staging",
            staging_dir=staging_rel,
            target_dir=target_rel,
            previous_installation_existed=previous_installation_existed,
        )
        try:
            write_journal(storage_root, transaction_id=transaction_id, record=journal_record)
        except (ValueError, OSError) as exc:
            return InstallationResult(
                status="failed", error=f"journal write failed: {exc}"
            )

        # Prepare staging directory
        staging_dir = storage_root / staging_rel
        staging_dir.mkdir(parents=True, exist_ok=True)

        backup_dir = None
        backup_rel = None

        try:
            # Copy files to staging, computing sizes and SHA-256 hashes
            copied_files = {}
            for entry in files:
                rel_path = entry.relative_to(source_dir)
                norm_path = str(rel_path).replace("\\", "/")
                if ".." in norm_path or norm_path.startswith("/"):
                    return InstallationResult(status="failed", error=f"Invalid path: {rel_path}")

                staging_file = staging_dir / rel_path
                staging_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry, staging_file)

                copied_files[norm_path] = {
                    "path": norm_path,
                    "size": staging_file.stat().st_size,
                    "sha256": _hash_file(staging_file, "sha256"),
                }

            manifest = {
                "schema_version": 2,
                "identifier": catalogued.identifier,
                "version": catalogued.version,
                "packaging": "directory",
                "installed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "entry_point": getattr(descriptor, "entry_point", None) or "index.py",
                "files": list(copied_files.values()),
                "source": {"type": "local", "local_path": str(source_dir)},
            }
            _validate_v2_manifest(manifest)

            # Transition journal to "publishing"
            publishing_record = make_journal_record(
                transaction_id=transaction_id,
                model_identifier=catalogued.identifier,
                version=catalogued.version,
                schema_version=2,
                state="publishing",
                staging_dir=staging_rel,
                target_dir=target_rel,
                previous_installation_existed=previous_installation_existed,
            )
            try:
                write_journal(storage_root, transaction_id=transaction_id, record=publishing_record)
            except (ValueError, OSError) as exc:
                return InstallationResult(
                    status="failed", error=f"journal publish write failed: {exc}"
                )

            # Publication: if previous installation exists, move target to backup
            if target_dir.exists():
                backup_dir = target_dir.parent / f"{target_dir.name}.backup.{uuid.uuid4().hex}"
                backup_rel = str(backup_dir.relative_to(storage_root))
                if not _is_safe_relative_path(backup_rel, base=storage_root):
                    return InstallationResult(
                        status="failed", error=f"backup path escapes storage root: {backup_rel}"
                    )
                shutil.move(target_dir, backup_dir)

                # Update journal with backup_dir
                publishing_with_backup = make_journal_record(
                    transaction_id=transaction_id,
                    model_identifier=catalogued.identifier,
                    version=catalogued.version,
                    schema_version=2,
                    state="publishing",
                    staging_dir=staging_rel,
                    target_dir=target_rel,
                    backup_dir=backup_rel,
                    previous_installation_existed=previous_installation_existed,
                )
                try:
                    write_journal(
                        storage_root, transaction_id=transaction_id, record=publishing_with_backup
                    )
                except (ValueError, OSError) as exc:
                    # Journal update failed but filesystem already changed; attempt rollback
                    try:
                        shutil.move(backup_dir, target_dir)
                    except Exception:
                        pass  # Preserve both dirs
                    return InstallationResult(
                        status="failed", error=f"journal backup write failed: {exc}"
                    )

                try:
                    shutil.move(staging_dir, target_dir)
                except Exception as e:
                    # Rollback: restore backup
                    try:
                        shutil.move(backup_dir, target_dir)
                    except Exception as rollback_e:
                        # Rollback restore failed — backup is preserved for
                        # recovery; journal remains available for recovery.
                        return InstallationResult(
                            status="failed",
                            error=(
                                f"publication failed: {e}; rollback failed: {rollback_e}; "
                                f"journal preserved at {transaction_id}"
                            ),
                        )
                    return InstallationResult(
                        status="failed",
                        error=f"publication failed: {e}; rolled back from backup",
                    )
            else:
                shutil.move(staging_dir, target_dir)

            # Write manifest to target
            _write_manifest(target_dir / MANIFEST_NAME, manifest)

            # Verify: all files exist with correct size and SHA-256
            verification_ok = True
            for file_info in manifest["files"]:
                file_path = target_dir / file_info["path"]
                if not file_path.is_file():
                    verification_ok = False
                    break
                actual_size = file_path.stat().st_size
                actual_sha256 = _hash_file(file_path, "sha256")
                if actual_size != file_info["size"] or actual_sha256 != file_info["sha256"]:
                    verification_ok = False
                    break

            if not verification_ok:
                # Rollback: remove partial target, restore backup if exists
                if backup_dir is not None and backup_dir.exists():
                    try:
                        _remove_dir(target_dir)
                        shutil.move(backup_dir, target_dir)
                    except Exception as rollback_e:
                        # Rollback restore failed — backup is preserved for
                        # recovery; journal remains available for recovery.
                        return InstallationResult(
                            status="failed",
                            error=(
                                f"verification failed; rollback failed: {rollback_e}; "
                                f"journal preserved at {transaction_id}"
                            ),
                        )
                # Mark journal failed
                try:
                    failed_record = make_journal_record(
                        transaction_id=transaction_id,
                        model_identifier=catalogued.identifier,
                        version=catalogued.version,
                        schema_version=2,
                        state="failed",
                        staging_dir=staging_rel,
                        target_dir=target_rel,
                        backup_dir=backup_rel,
                        previous_installation_existed=previous_installation_existed,
                        error="manifest verification failed",
                    )
                    write_journal(storage_root, transaction_id=transaction_id, record=failed_record)
                except (ValueError, OSError):
                    pass  # Best-effort journal update
                return InstallationResult(status="failed", error="manifest verification failed")

            # Success: mark journal committed
            committed_record = make_journal_record(
                transaction_id=transaction_id,
                model_identifier=catalogued.identifier,
                version=catalogued.version,
                schema_version=2,
                state="committed",
                staging_dir=staging_rel,
                target_dir=target_rel,
                backup_dir=backup_rel,
                previous_installation_existed=previous_installation_existed,
            )
            try:
                write_journal(storage_root, transaction_id=transaction_id, record=committed_record)
            except (ValueError, OSError) as exc:
                # Journal commit failed: do not delete the backup.  The
                # installation is valid on disk but the journal is not
                # committed, so preserve both for future recovery and report
                # failure rather than claiming a fully committed transaction.
                return InstallationResult(
                    status="failed",
                    error=(
                        f"journal commit failed: {exc}; "
                        f"installation on disk is valid but not committed; "
                        f"backup and journal preserved for recovery at {transaction_id}"
                    ),
                )

            # Remove backup only after successful verification and commit
            if backup_dir is not None:
                _remove_dir(backup_dir)

            return InstallationResult(
                status="installed",
                artifact_path=target_dir,
                manifest_path=target_dir / MANIFEST_NAME,
            )

        except Exception as e:
            # Attempt rollback if backup exists
            if backup_dir is not None and backup_dir.exists():
                try:
                    _remove_dir(target_dir)
                    shutil.move(backup_dir, target_dir)
                except Exception:
                    pass  # Backup preserved for recovery; journal remains available.
            return InstallationResult(status="failed", error=str(e))

        finally:
            # Clean up staging directory if it still exists (no-op if already moved)
            _remove_dir(staging_dir)



def _normalize_checksum(checksum: str) -> Tuple[str, str]:
    """Return ``(algorithm, lowercase_digest)`` for a descriptor checksum."""
    value = checksum.strip()
    if ":" in value:
        algorithm, digest = value.split(":", 1)
        algorithm = algorithm.strip().lower()
    else:
        algorithm, digest = "sha256", value
    digest = digest.strip().lower()
    if algorithm not in _SUPPORTED_CHECKSUMS:
        raise ValueError(f"Unsupported checksum algorithm: {algorithm!r}")
    expected_length = _SUPPORTED_CHECKSUMS[algorithm]
    if len(digest) != expected_length or re.fullmatch(r"[0-9a-f]+", digest) is None:
        raise ValueError(
            f"Invalid {algorithm} checksum: expected {expected_length} hex characters"
        )
    return algorithm, digest


def _artifact_name(download_url: Optional[str]) -> str:
    """Return a fixed artifact filename, keeping a safe URL suffix."""
    suffix = Path(urlparse(download_url or "").path).suffix
    if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix or "") is not None:
        return f"artifact{suffix}"
    return "artifact"


def _hash_file(path: Path, algorithm: str) -> str:
    hasher = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _remove_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _validate_filesystem_entry(entry: Path, source_dir: Path) -> Optional[str]:
    """
    Validate a filesystem entry for safety without following symlinks/junctions.

    Returns an error message if the entry is unsafe, None if safe.
    """
    try:
        # Use lstat to avoid following symlinks/junctions
        st = entry.lstat()
    except OSError as e:
        return f"Cannot stat {entry}: {e}"

    # Check for symbolic links
    if stat.S_ISLNK(st.st_mode):
        return f"Symbolic link not allowed: {entry}"

    # Check for Windows junctions (available on Python 3.12+)
    if hasattr(entry, "is_junction"):
        try:
            if entry.is_junction():
                return f"Windows junction not allowed: {entry}"
        except OSError:
            return f"Windows junction detection failed (fail closed): {entry}"

    # Check for Windows reparse points (FILE_ATTRIBUTE_REPARSE_POINT = 0x400)
    if hasattr(st, "st_file_attributes"):
        if st.st_file_attributes & 0x400:
            return f"Windows reparse point not allowed: {entry}"

    # Only allow regular files and directories
    if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
        return f"Non-regular filesystem entry not allowed: {entry}"

    return None


def _collect_and_validate_source_tree(source_dir: Path) -> Tuple[List[Path], List[Path], Optional[str]]:
    """
    Walk the source directory tree, validate all entries, and return lists of files and directories.

    Uses explicit traversal with os.scandir() to avoid following symlinks/junctions.
    Validates each entry without following links before descending into a directory.
    Rejects symlinks, junctions, reparse points, and non-regular entries.
    If junction detection raises an error, fails closed rather than assuming the entry is safe.
    Preserves the existing behaviour of collecting all valid files before copying any of them.

    Returns (files, dirs, error_message).
    """
    files = []
    dirs = []

    # Use scandir for efficient, non-following directory traversal
    try:
        entries = list(source_dir.iterdir())
    except OSError as e:
        return [], [], f"Cannot read source directory {source_dir}: {e}"

    for entry in entries:
        # Validate entry without following links
        error = _validate_filesystem_entry(entry, source_dir)
        if error:
            return [], [], error

        if entry.is_file():
            files.append(entry)
        elif entry.is_dir():
            # Recurse into validated subdirectory
            try:
                sub_files, sub_dirs, sub_error = _collect_and_validate_source_tree(entry)
                if sub_error:
                    return [], [], sub_error
                files.extend(sub_files)
                dirs.extend(sub_dirs)
            except (OSError, ValueError) as e:
                return [], [], f"Error traversing {entry}: {e}"

    return files, dirs, None


def _read_manifest(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_manifest(path: Path, data: dict) -> None:
    """Write a versioned manifest with proper schema validation."""
    try:
        if "schema_version" in data:
            _validate_v2_manifest(data)
        else:
            _validate_v1_manifest(data)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid manifest: {exc}") from exc

    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        _remove_file(temporary)