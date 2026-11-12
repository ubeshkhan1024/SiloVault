"""Local filesystem storage adapter for SiloVault.

Implements :class:`~backend.storage.interfaces.StorageOperations` for the
local provider.  ``LocalAdapter`` performs physical filesystem operations
only — it never touches the database, never imports FastAPI or SQLAlchemy,
never writes activity records, and never implements
:class:`~backend.storage.interfaces.StorageLifecycle` (authentication,
token refresh, and disconnection are meaningless for a local filesystem).

Storage conventions
-------------------
* Files are stored **flat** directly inside the configured storage root
  (``settings.uploads_dir`` in the application, injected via the constructor
  here).  This matches the existing upload endpoint's convention.
* The physical storage filename is a server-generated UUID (with the original
  extension preserved).  ``provider_file_id`` is that storage filename.
* ``name`` and ``logical_path`` are metadata carried in the returned
  :class:`~backend.storage.types.ProviderFileRecord`; they are never used to
  build filesystem paths, so client-supplied values cannot cause path
  traversal.

Safe writing
------------
Uploads are written to a hidden ``.<uuid>.part`` temporary file in the same
directory, fsync'd, then atomically published to the final UUID filename via
``os.link`` (hard link), which fails with ``FileExistsError`` on all
platforms if the destination exists.  An interrupted upload therefore never
leaves a partially written destination file, and an upload can never
silently overwrite an existing file.  If the filesystem does not support
hard-link publication, the upload fails with a normalized storage error
rather than falling back to an overwrite-capable operation.

Deletion safety protocol (quarantine / restore / cleanup)
---------------------------------------------------------
Quarantine state is **filesystem-local and persistent** — it survives adapter
recreation and process restarts without any database involvement:

* ``<storage_root>/.quarantine/<quarantine_id>/<provider_file_id>``
  — the quarantined file itself.
* ``<storage_root>/.quarantine/<quarantine_id>/meta.json``
  — a sidecar metadata file recording the original ``provider_file_id``,
  the file size, and the quarantine timestamp.

``quarantine()`` creates the entry directory, moves the file in, then writes
the sidecar **atomically** (temp file + fsync + rename).  A crash between the
file move and the metadata write leaves an entry with the quarantined file
but no sidecar; :meth:`restore` recovers such entries by identifying the
quarantined file directly from the entry directory (there is exactly one
non-metadata file there) instead of trusting the sidecar.

``restore()`` never trusts the ``provider_file_id`` read from the sidecar
without validation: the value must be a plain, safe storage identifier
(no path separators, no traversal, no reserved names) and the resulting
destination must remain strictly inside the storage root.  Tampered or
malformed metadata therefore cannot redirect a restore outside the root.
Restore also refuses to overwrite anything that now occupies the original
path — the quarantined copy is left in place in that case so no data is
lost.

Quarantine IDs are random 32-character hex strings; anything that does not
match that shape is rejected before any filesystem access, which also makes
quarantine operations immune to path traversal.

Security
--------
Every operation that resolves a physical path from a caller-supplied
identifier runs it through :meth:`LocalAdapter._resolve_inside_root`, which
rejects unsafe identifiers (separators, traversal, reserved internal paths)
and requires the resolved path to remain strictly inside the storage root.
Symlinks are inspected on the **lexical** entry (before ``resolve()``
follows them), so quarantine refuses symlinks regardless of where they
point — inside or outside the storage root.  The ``.quarantine``
directory and every quarantine entry are likewise inspected without
following symlinks: a symlinked ``.quarantine``, a symlinked entry
directory, or a symlinked ``meta.json`` is rejected outright, and
``mkdir(parents=True)`` is never allowed to follow a redirected
``.quarantine`` path.  All failures are normalized into the storage error
hierarchy with ``provider="LOCAL"``.
"""

from __future__ import annotations

import dataclasses
import errno
import json
import mimetypes
import os
import re
import shutil
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .errors import (
    StorageConflictError,
    StorageError,
    StorageNotFoundError,
    StoragePermissionError,
    StorageUnavailableError,
)
from .interfaces import StorageOperations
from .types import ProviderFileRecord

#: Hidden directory (inside the storage root) holding quarantine entries.
_QUARANTINE_DIRNAME = ".quarantine"

#: Name of the sidecar metadata file inside each quarantine entry.
_QUARANTINE_META_FILENAME = "meta.json"

#: Quarantine IDs are random hex strings generated by this adapter.  Anything
#: else is rejected outright (this also blocks traversal into the quarantine
#: tree).
_QUARANTINE_ID_PATTERN = re.compile(r"^[0-9a-f]{16,64}$")

#: Sanity cap on the extension carried over from the original filename.
_MAX_EXTENSION_LENGTH = 50


class LocalAdapter(StorageOperations):
    """StorageOperations implementation backed by the local filesystem.

    Parameters
    ----------
    storage_root : str | Path
        Directory in which files are stored (e.g. the application's
        ``uploads`` directory).  Created if it does not exist.
    """

    def __init__(self, storage_root: str | Path) -> None:
        self._root = Path(storage_root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def storage_root(self) -> Path:
        """The resolved absolute storage root directory."""
        return self._root

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _validate_storage_id(provider_file_id: str) -> None:
        """Reject storage identifiers that are unsafe to use as file names.

        A storage identifier must be a single, plain file name: no path
        separators, no traversal components, no reserved internal names
        (dotfiles such as the quarantine directory or part files), no
        empty or whitespace-padded values.

        Raises
        ------
        StorageNotFoundError
            If the identifier is not a safe storage identifier.
        """
        if not isinstance(provider_file_id, str) or not provider_file_id:
            raise StorageNotFoundError(
                provider_file_id if isinstance(provider_file_id, str) else "",
                provider="LOCAL",
            )
        if provider_file_id != provider_file_id.strip():
            raise StorageNotFoundError(provider_file_id, provider="LOCAL")
        if (
            "\x00" in provider_file_id
            or "/" in provider_file_id
            or "\\" in provider_file_id
            or ".." in provider_file_id
        ):
            raise StorageNotFoundError(provider_file_id, provider="LOCAL")
        stem = provider_file_id.split(".", 1)[0]
        if stem.startswith("."):
            # Reserved internal names: the quarantine directory, part files,
            # and any other dotfile.
            raise StorageNotFoundError(provider_file_id, provider="LOCAL")

    def _resolve_inside_root(self, provider_file_id: str) -> tuple[Path, Path]:
        """Resolve a storage filename strictly inside the storage root.

        The identifier is validated as a safe file name first, then the
        resolved path must remain inside the root.  Returns a tuple of
        ``(lexical, resolved)`` where *lexical* is the path as constructed
        (used for symlink inspection before following links) and *resolved*
        is the fully resolved path (used for existence checks and I/O).

        Raises
        ------
        StorageNotFoundError
            If the identifier is unsafe or the resolved path escapes the
            storage root.
        """
        self._validate_storage_id(provider_file_id)
        lexical = self._root / provider_file_id
        candidate = lexical.resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise StorageNotFoundError(provider_file_id, provider="LOCAL") from exc
        return lexical, candidate

    def _inspect_quarantine_dir(self, quarantine_dir: Path) -> None:
        """Inspect .quarantine directory without following symlinks.

        Raises
        ------
        StorageNotFoundError
            If .quarantine exists and is a symlink, or exists and is not a directory.
        StoragePermissionError
            If permission is denied inspecting the directory.
        StorageUnavailableError
            On other filesystem errors during inspection.
        """
        try:
            st = quarantine_dir.lstat()
        except FileNotFoundError:
            return  # doesn't exist yet; that's fine
        except PermissionError as exc:
            raise StoragePermissionError(
                detail="Permission denied inspecting quarantine directory",
                provider="LOCAL",
            ) from exc
        except OSError as exc:
            raise StorageUnavailableError(
                detail=f"Failed to inspect quarantine directory: {exc.strerror or 'unknown error'}",
                provider="LOCAL",
            ) from exc
        if stat.S_ISLNK(st.st_mode):
            raise StorageNotFoundError(
                "quarantine directory is a symlink", provider="LOCAL"
            )
        if not stat.S_ISDIR(st.st_mode):
            raise StorageNotFoundError(
                "quarantine path exists but is not a directory", provider="LOCAL"
            )

    def _resolve_quarantine_entry(self, quarantine_id: str) -> Path:
        """Resolve a quarantine entry directory from an opaque quarantine ID.

        The ID must be a plain hex token; the resolved directory must stay
        inside the storage root.  The .quarantine directory and the entry
        directory must be actual directories, not symlinks.

        Raises
        ------
        StorageNotFoundError
            If the ID is malformed or the resolved path escapes the root,
            or if .quarantine or the entry directory is a symlink.
        """
        if not isinstance(quarantine_id, str) or not _QUARANTINE_ID_PATTERN.fullmatch(
            quarantine_id
        ):
            raise StorageNotFoundError(
                quarantine_id if isinstance(quarantine_id, str) else "",
                provider="LOCAL",
            )
        quarantine_dir = self._root / _QUARANTINE_DIRNAME
        entry_dir = quarantine_dir / quarantine_id

        # Inspect .quarantine without following symlinks
        self._inspect_quarantine_dir(quarantine_dir)

        # Reject symlinked entry directory (non-following check via lstat)
        try:
            st = entry_dir.lstat()
            if stat.S_ISLNK(st.st_mode):
                raise StorageNotFoundError(quarantine_id, provider="LOCAL")
            if not stat.S_ISDIR(st.st_mode):
                raise StorageNotFoundError(quarantine_id, provider="LOCAL")
        except FileNotFoundError as exc:
            raise StorageNotFoundError(quarantine_id, provider="LOCAL") from exc
        except PermissionError as exc:
            raise StoragePermissionError(
                detail="Permission denied inspecting quarantine entry",
                provider="LOCAL",
            ) from exc
        except OSError as exc:
            raise StorageUnavailableError(
                detail=f"Failed to inspect quarantine entry: {exc.strerror or 'unknown error'}",
                provider="LOCAL",
            ) from exc

        # Resolve and verify containment
        entry = entry_dir.resolve()
        try:
            entry.relative_to(self._root)
        except ValueError as exc:
            raise StorageNotFoundError(quarantine_id, provider="LOCAL") from exc
        return entry

    @staticmethod
    def _pick_extension(logical_path: str, name: str) -> str:
        """Pick the file extension to carry over from the original filename."""
        for source in (logical_path, name):
            if not source:
                continue
            ext = Path(source).suffix
            if ext and len(ext) <= _MAX_EXTENSION_LENGTH:
                return ext
        return ""

    @staticmethod
    def _guess_mime(*candidates: str) -> str:
        """Guess a MIME type from filename candidates, with a safe fallback."""
        for candidate in candidates:
            if not candidate:
                continue
            mime, _ = mimetypes.guess_type(candidate)
            if mime:
                return mime
        return "application/octet-stream"

    def _describe(
        self,
        path: Path,
        provider_file_id: str,
        *,
        name: str | None = None,
        logical_path: str | None = None,
    ) -> ProviderFileRecord:
        """Build a ProviderFileRecord from a physical file on disk.

        ``name`` and ``logical_path`` are metadata; when unknown (e.g. during
        restore, where only the physical filename is persisted) they fall
        back to the storage filename.  ``sha256`` is left ``None`` — the
        authoritative hash is the one registered in the database.
        """
        stat = path.stat()
        return ProviderFileRecord(
            provider_file_id=provider_file_id,
            name=name or provider_file_id,
            logical_path=logical_path or f"/{provider_file_id}",
            mime_type=self._guess_mime(path.name, name or ""),
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        )

    def _read_quarantine_meta(self, entry_dir: Path, quarantine_id: str) -> dict | None:
        """Read and validate the quarantine sidecar metadata file.

        Returns the validated metadata dict, or ``None`` when the sidecar is
        missing (an interrupted quarantine) or unreadable/malformed (tampered
        metadata).  Callers must treat ``None`` as "the sidecar cannot be
        trusted" and fall back to directory-based recovery.

        A symlinked or non-regular meta.json is explicitly rejected with
        StoragePermissionError (not silently treated as missing).  Fallback
        recovery is preserved only for genuinely missing or malformed regular
        files.

        Raises
        ------
        StorageNotFoundError
            If the entry directory itself does not exist.
        StoragePermissionError
            If meta.json is a symlink, not a regular file, or permission denied.
        StorageUnavailableError
            On other filesystem errors during read.

        Notes
        -----
        On POSIX, ``O_NOFOLLOW`` at open time provides race-free symlink
        rejection.  On Windows, ``O_NOFOLLOW`` does not prevent following
        symlinks; we perform an explicit ``lstat()`` pre-check, but a TOCTOU
        window exists between the check and the open.  This is a platform
        limitation; the pre-check provides defense-in-depth but not a guarantee.
        """
        if not entry_dir.is_dir():
            raise StorageNotFoundError(quarantine_id, provider="LOCAL")
        meta_path = entry_dir / _QUARANTINE_META_FILENAME

        # Pre-check with lstat to reject symlinks on all platforms.
        # On POSIX, O_NOFOLLOW provides the guarantee at open time.
        # On Windows, O_NOFOLLOW does not prevent following symlinks,
        # so we must check explicitly before opening.  A TOCTOU window
        # exists between this check and the open on Windows.
        try:
            st = meta_path.lstat()
            if stat.S_ISLNK(st.st_mode):
                raise StoragePermissionError(
                    detail="Quarantine metadata is a symlink; refusing to read",
                    provider="LOCAL",
                )
        except FileNotFoundError:
            return None  # missing metadata (interrupted quarantine)
        except PermissionError as exc:
            raise StoragePermissionError(
                detail="Permission denied inspecting quarantine metadata",
                provider="LOCAL",
            ) from exc
        except OSError as exc:
            raise StorageUnavailableError(
                detail=f"Failed to inspect quarantine metadata: {exc.strerror or 'unknown error'}",
                provider="LOCAL",
            ) from exc

        # Open the file (O_NOFOLLOW provides race-free guarantee on POSIX;
        # on Windows we already rejected symlinks above, but a TOCTOU window
        # remains between the lstat check and this open).
        nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(meta_path, os.O_RDONLY | nofollow_flag)
        except FileNotFoundError:
            return None  # missing metadata (interrupted quarantine)
        except PermissionError as exc:
            raise StoragePermissionError(
                detail="Permission denied reading quarantine metadata",
                provider="LOCAL",
            ) from exc
        except OSError as exc:
            raise StorageUnavailableError(
                detail=f"Failed to open quarantine metadata: {exc.strerror or 'unknown error'}",
                provider="LOCAL",
            ) from exc

        try:
            # Verify it's a regular file via fstat on the open fd
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise StoragePermissionError(
                    detail="Quarantine metadata is not a regular file; refusing to read",
                    provider="LOCAL",
                )
            # Read from the already-open fd. We manage the file object manually
            # to retain explicit control over fd ownership and error mapping.
            f = os.fdopen(fd, "r", encoding="utf-8")
            fd = -1  # ownership transferred to file object
            try:
                content = f.read()
            except PermissionError as exc:
                f.close()
                raise StoragePermissionError(
                    detail="Permission denied reading quarantine metadata",
                    provider="LOCAL",
                ) from exc
            except OSError as exc:
                f.close()
                raise StorageUnavailableError(
                    detail=f"Failed to read quarantine metadata: {exc.strerror or 'unknown error'}",
                    provider="LOCAL",
                ) from exc
            else:
                f.close()
        except StoragePermissionError:
            # Non-regular file: we still own the fd, close it
            if fd != -1:
                os.close(fd)
            raise
        except OSError as exc:
            # fstat failed: we still own the fd, close it
            if fd != -1:
                os.close(fd)
            raise StorageUnavailableError(
                detail=f"Failed to inspect quarantine metadata: {exc.strerror or 'unknown error'}",
                provider="LOCAL",
            ) from exc
        except Exception:
            # Any other exception: we still own the fd, close it
            if fd != -1:
                os.close(fd)
            raise

        try:
            meta = json.loads(content)
        except json.JSONDecodeError:
            return None
        if not isinstance(meta, dict):
            return None
        provider_file_id = meta.get("provider_file_id")
        if not isinstance(provider_file_id, str) or not provider_file_id:
            return None
        # The sidecar value is untrusted input: it must be a safe storage
        # identifier before it may influence any filesystem path.
        try:
            self._validate_storage_id(provider_file_id)
        except StorageNotFoundError:
            return None
        size_bytes = meta.get("size_bytes")
        if size_bytes is not None and (
            isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0
        ):
            return None
        quarantined_at = meta.get("quarantined_at")
        if quarantined_at is not None and not isinstance(quarantined_at, str):
            return None
        return meta

    def _recover_quarantine_file(
        self, entry_dir: Path, quarantine_id: str, meta: dict | None
    ) -> tuple[str, Path]:
        """Locate the quarantined file for an entry, trusting the sidecar only
        when it is valid.

        When the sidecar is missing or untrusted, the file is identified
        directly from the entry directory: a valid quarantine entry contains
        exactly one regular file that is not the metadata sidecar.  Its name
        is validated as a safe storage identifier before use.  Symlinks are
        never accepted as quarantined files.

        Returns
        -------
        tuple[str, Path]
            The validated storage identifier and the path of the
            quarantined file inside the entry directory.

        Raises
        ------
        StorageNotFoundError
            If no (or more than one) candidate file can be identified.
        """
        if meta is not None:
            provider_file_id = meta["provider_file_id"]
            candidate = entry_dir / provider_file_id
            if candidate.is_file() and not candidate.is_symlink():
                return provider_file_id, candidate
            # Sidecar claims a file that is not actually there — fall back to
            # directory-based identification rather than trusting it.

        files = [
            p
            for p in entry_dir.iterdir()
            if p.name != _QUARANTINE_META_FILENAME and p.is_file() and not p.is_symlink()
        ]
        if len(files) != 1:
            raise StorageNotFoundError(
                quarantine_id,
                provider="LOCAL",
            )
        provider_file_id = files[0].name
        self._validate_storage_id(provider_file_id)
        return provider_file_id, files[0]

    # -----------------------------------------------------------------------
    # StorageOperations
    # -----------------------------------------------------------------------

    def upload(self, file_data: bytes, name: str, logical_path: str) -> ProviderFileRecord:
        """Store a file in the storage root under a server-generated UUID name.

        The write goes to a hidden temporary file first and is atomically
        published, so an interrupted upload never produces a partially written
        destination file.  Publication uses a cross-platform no-overwrite
        strategy: attempt ``os.link`` (fails with ``FileExistsError`` if the
        destination exists on all platforms), then unlink the temp file.  This
        guarantees no silent overwrite on Windows or POSIX.

        Raises
        ------
        StorageConflictError
            If the generated storage filename already exists.
        StoragePermissionError
            If the storage root is not writable.
        StorageUnavailableError
            On any other filesystem failure during the write.
        """
        if not isinstance(file_data, (bytes, bytearray)):
            raise TypeError("file_data must be bytes")
        # Use hex UUID (32 chars, no hyphens) for consistent storage filenames.
        storage_filename = f"{uuid.uuid4().hex}{self._pick_extension(logical_path, name)}"
        final_path = self._root / storage_filename
        if final_path.exists():
            raise StorageConflictError(
                detail=f"Storage filename collision: {storage_filename!r}",
                provider="LOCAL",
            )

        temp_path = self._root / f".{uuid.uuid4().hex}.part"
        try:
            with temp_path.open("wb") as handle:
                handle.write(bytes(file_data))
                handle.flush()
                os.fsync(handle.fileno())
            # Cross-platform atomic no-overwrite publication:
            # os.link fails with FileExistsError if destination exists (Windows & POSIX).
            try:
                os.link(temp_path, final_path)
            except FileExistsError as exc:
                raise StorageConflictError(
                    detail=f"Storage filename collision: {storage_filename!r}",
                    provider="LOCAL",
                ) from exc
            except PermissionError as exc:
                # Hard-link publication denied by filesystem permissions.
                self._remove_temp_safely(temp_path)
                raise StoragePermissionError(
                    detail="Storage root is not writable",
                    provider="LOCAL",
                ) from exc
            except OSError as exc:
                # Hard-link publication unsupported (e.g. some network filesystems).
                # Do NOT fall back to replace() — it can overwrite on POSIX.
                # Clean up temp file and raise a normalized error.
                self._remove_temp_safely(temp_path)
                raise StorageUnavailableError(
                    detail=(
                        "Atomic no-overwrite publication not supported on this filesystem. "
                        "Upload cannot proceed without overwrite guarantee."
                    ),
                    provider="LOCAL",
                ) from exc
            finally:
                # Best-effort cleanup of the temp file (already linked/unlinked).
                self._remove_temp_safely(temp_path)
        except PermissionError as exc:
            self._remove_temp_safely(temp_path)
            raise StoragePermissionError(
                detail="Storage root is not writable",
                provider="LOCAL",
            ) from exc
        except StorageError:
            self._remove_temp_safely(temp_path)
            raise
        except OSError as exc:
            self._remove_temp_safely(temp_path)
            raise StorageUnavailableError(
                detail=f"Failed to write uploaded file: {exc.strerror or 'unknown error'}",
                provider="LOCAL",
            ) from exc

        return self._describe(final_path, storage_filename, name=name, logical_path=logical_path)

    def download(self, provider_file_id: str) -> bytes:
        """Return the raw bytes of a stored file.

        Raises
        ------
        StorageNotFoundError
            If the file does not exist inside the storage root.
        StoragePermissionError
            If the file cannot be read.
        StorageUnavailableError
            On any other filesystem failure.
        """
        lexical, resolved = self._resolve_inside_root(provider_file_id)
        if lexical.is_symlink():
            raise StorageNotFoundError(provider_file_id, provider="LOCAL")
        if not resolved.is_file():
            raise StorageNotFoundError(provider_file_id, provider="LOCAL")
        try:
            return resolved.read_bytes()
        except PermissionError as exc:
            raise StoragePermissionError(provider="LOCAL") from exc
        except OSError as exc:
            raise StorageUnavailableError(provider="LOCAL") from exc

    def rename(self, provider_file_id: str, new_name: str) -> ProviderFileRecord:
        """Rename a file's user-facing name.

        The physical storage filename is immutable (it is the
        ``provider_file_id``); renaming updates the user-facing ``name``
        carried in the returned record.  The service layer persists the new
        name in the database.

        Raises
        ------
        StorageNotFoundError
            If the file does not exist inside the storage root.
        """
        lexical, resolved = self._resolve_inside_root(provider_file_id)
        if lexical.is_symlink() or not resolved.is_file():
            raise StorageNotFoundError(provider_file_id, provider="LOCAL")
        record = self._describe(resolved, provider_file_id)
        return dataclasses.replace(record, name=new_name)

    def move(self, source_provider_file_id: str, target_logical_path: str) -> ProviderFileRecord:
        """Move a file to a different logical path.

        For local storage the logical path is a virtual namespace; the
        physical file stays in place and the service layer persists the new
        logical path in the database.

        Raises
        ------
        StorageNotFoundError
            If the file does not exist inside the storage root.
        """
        lexical, resolved = self._resolve_inside_root(source_provider_file_id)
        if lexical.is_symlink() or not resolved.is_file():
            raise StorageNotFoundError(source_provider_file_id, provider="LOCAL")
        record = self._describe(resolved, source_provider_file_id)
        return dataclasses.replace(record, logical_path=target_logical_path)

    def quarantine(self, provider_file_id: str) -> str:
        """Move a file into the quarantine area and return its quarantine ID.

        The file is moved to ``<root>/.quarantine/<id>/<provider_file_id>``
        and a ``meta.json`` sidecar records the original storage filename so
        that :meth:`restore` and :meth:`cleanup` can locate it later — even
        after the adapter (or the process) has been restarted.  The sidecar
        is written atomically (temp file + fsync + rename); if the process
        dies between the file move and the metadata write, :meth:`restore`
        recovers the entry from the file's own name.

        Raises
        ------
        StorageNotFoundError
            If the file does not exist inside the storage root.
        StoragePermissionError
            If the file is a symlink or the move is denied by the OS.
        StorageUnavailableError
            On any other filesystem failure.
        """
        # First validate the ID format (no traversal, no separators, etc.)
        # WITHOUT resolving the path.  This lets us inspect the lexical
        # entry for symlinks before any containment check.
        self._validate_storage_id(provider_file_id)
        lexical = self._root / provider_file_id

        # Inspect the lexical entry BEFORE following symlinks: a symlink is
        # refused no matter where it points (inside or outside the root).
        if lexical.is_symlink():
            raise StoragePermissionError(
                detail="Refusing to quarantine a symlink",
                provider="LOCAL",
            )

        # Now resolve and verify containment.
        try:
            resolved = lexical.resolve()
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise StorageNotFoundError(provider_file_id, provider="LOCAL") from exc

        if not resolved.is_file():
            raise StorageNotFoundError(provider_file_id, provider="LOCAL")
        size_bytes = resolved.stat().st_size

        quarantine_id = uuid.uuid4().hex
        entry_dir = self._root / _QUARANTINE_DIRNAME / quarantine_id
        quarantined_file = entry_dir / provider_file_id

        # Inspect .quarantine lexically BEFORE mkdir(parents=True) to prevent
        # following a symlinked .quarantine path.
        quarantine_dir = self._root / _QUARANTINE_DIRNAME
        self._inspect_quarantine_dir(quarantine_dir)

        try:
            entry_dir.mkdir(parents=True)
        except PermissionError as exc:
            raise StoragePermissionError(
                detail="Permission denied creating quarantine entry",
                provider="LOCAL",
            ) from exc
        except OSError as exc:
            raise StorageUnavailableError(
                detail=f"Failed to create quarantine entry: {exc.strerror or 'unknown error'}",
                provider="LOCAL",
            ) from exc

        # Re-validate the source immediately before moving to catch symlink
        # substitution between the earlier check and now. Use a no-follow open
        # where supported to ensure we're still operating on a regular file.
        # Pre-check with lstat to reject symlinks on all platforms.
        try:
            st = lexical.lstat()
            if stat.S_ISLNK(st.st_mode):
                self._remove_empty_dir(entry_dir)
                raise StoragePermissionError(
                    detail="Source path was replaced with a symlink",
                    provider="LOCAL",
                )
        except FileNotFoundError as exc:
            self._remove_empty_dir(entry_dir)
            raise StorageNotFoundError(provider_file_id, provider="LOCAL") from exc
        except PermissionError as exc:
            self._remove_empty_dir(entry_dir)
            raise StoragePermissionError(provider="LOCAL") from exc
        except OSError as exc:
            self._remove_empty_dir(entry_dir)
            raise StorageUnavailableError(
                detail=f"Failed to re-validate source file: {exc.strerror or 'unknown error'}",
                provider="LOCAL",
            ) from exc

        # Open the file (O_NOFOLLOW provides race-free guarantee on POSIX;
        # on Windows O_NOFOLLOW does not prevent following symlinks at open time.
        # We perform an lstat() pre-check above, but a TOCTOU window exists
        # between that check and the subsequent path-based rename().
        # This is a platform limitation; the pre-check provides defense-in-depth
        # but not a guarantee against symlink substitution on Windows.
        #
        # On POSIX, O_NOFOLLOW at open time guarantees we opened the real file,
        # but the subsequent rename() operates on the path, not the fd.
        # A TOCTOU window exists between close() and rename() on all platforms.
        # An additional lstat() check immediately before rename narrows this
        # window but cannot eliminate it.
        nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(lexical, os.O_RDONLY | nofollow_flag)
        except FileNotFoundError as exc:
            self._remove_empty_dir(entry_dir)
            raise StorageNotFoundError(provider_file_id, provider="LOCAL") from exc
        except PermissionError as exc:
            self._remove_empty_dir(entry_dir)
            raise StoragePermissionError(provider="LOCAL") from exc
        except OSError as exc:
            self._remove_empty_dir(entry_dir)
            raise StorageUnavailableError(
                detail=f"Failed to re-validate source file: {exc.strerror or 'unknown error'}",
                provider="LOCAL",
            ) from exc
        else:
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise StoragePermissionError(
                        detail="Source path is not a regular file",
                        provider="LOCAL",
                    )
            finally:
                os.close(fd)

        # Defense-in-depth: re-check the source is still a regular file
        # immediately before the path-based rename.  On POSIX, O_NOFOLLOW at
        # open time guaranteed we opened the real file, but the subsequent
        # rename() operates on the path, not the fd.  A TOCTOU window exists
        # between close() and rename() on all platforms.  This lstat check
        # narrows the window but cannot eliminate it.
        try:
            st = lexical.lstat()
            if stat.S_ISLNK(st.st_mode):
                self._remove_empty_dir(entry_dir)
                raise StoragePermissionError(
                    detail="Source path was replaced with a symlink before rename",
                    provider="LOCAL",
                )
            if not stat.S_ISREG(st.st_mode):
                self._remove_empty_dir(entry_dir)
                raise StoragePermissionError(
                    detail="Source path is no longer a regular file",
                    provider="LOCAL",
                )
        except FileNotFoundError as exc:
            self._remove_empty_dir(entry_dir)
            raise StorageNotFoundError(provider_file_id, provider="LOCAL") from exc
        except PermissionError as exc:
            self._remove_empty_dir(entry_dir)
            raise StoragePermissionError(provider="LOCAL") from exc
        except OSError as exc:
            self._remove_empty_dir(entry_dir)
            raise StorageUnavailableError(
                detail=f"Failed to re-validate source file before rename: {exc.strerror or 'unknown error'}",
                provider="LOCAL",
            ) from exc

        try:
            lexical.rename(quarantined_file)
        except PermissionError as exc:
            self._remove_empty_dir(entry_dir)
            raise StoragePermissionError(provider="LOCAL") from exc
        except OSError as exc:
            self._remove_empty_dir(entry_dir)
            raise StorageUnavailableError(
                detail=f"Failed to stage file for deletion: {exc.strerror or 'unknown error'}",
                provider="LOCAL",
            ) from exc

        meta = {
            "provider_file_id": provider_file_id,
            "size_bytes": size_bytes,
            "quarantined_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._write_meta_atomic(entry_dir, meta)
        except OSError as exc:
            # The sidecar is what makes restoration reliable — if it cannot be
            # written, put the file back and report the failure.  (Note: even
            # if this rollback also fails, the file remains recoverable via
            # the directory-based fallback in restore().)
            self._restore_after_failed_meta(quarantined_file, lexical, entry_dir)
            # If rollback succeeded, the entry directory should now be empty;
            # remove it to avoid leaving orphaned quarantine directories.
            self._remove_empty_dir(entry_dir)
            raise StorageUnavailableError(
                detail=f"Failed to write quarantine metadata: {exc.strerror or 'unknown error'}",
                provider="LOCAL",
            ) from exc

        return quarantine_id

    def restore(self, quarantine_id: str) -> ProviderFileRecord:
        """Restore a quarantined file to its original location.

        The destination is derived from the quarantined file's name (from a
        valid sidecar, or from the entry directory when the sidecar is
        missing or untrusted) and is validated as a safe storage identifier
        that stays strictly inside the storage root.  Refuses to overwrite
        anything that now occupies the original path — the quarantined copy
        is left in place in that case so no data is lost.

        Raises
        ------
        StorageNotFoundError
            If the quarantine entry (or its quarantined file) does not exist.
        StorageConflictError
            If the original path is occupied again.
        StoragePermissionError / StorageUnavailableError
            On filesystem failures during the restore.
        """
        entry_dir = self._resolve_quarantine_entry(quarantine_id)
        # Re-verify after resolution: a race could replace the entry with a
        # symlink between _resolve_quarantine_entry and here. Fail closed.
        try:
            st = entry_dir.lstat()
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise StorageNotFoundError(quarantine_id, provider="LOCAL")
        except FileNotFoundError as exc:
            raise StorageNotFoundError(quarantine_id, provider="LOCAL") from exc
        except PermissionError as exc:
            raise StoragePermissionError(
                detail="Permission denied inspecting quarantine entry",
                provider="LOCAL",
            ) from exc
        except OSError as exc:
            raise StorageUnavailableError(
                detail=f"Failed to inspect quarantine entry: {exc.strerror or 'unknown error'}",
                provider="LOCAL",
            ) from exc
        meta = self._read_quarantine_meta(entry_dir, quarantine_id)
        provider_file_id, quarantined_file = self._recover_quarantine_file(
            entry_dir, quarantine_id, meta
        )

        target = self._root / provider_file_id
        # Check for symlink at the lexical target FIRST, before resolving.
        # This prevents following a symlink that points outside the root.
        if target.is_symlink():
            raise StorageConflictError(
                detail=(
                    f"Cannot restore: original path is a symlink; "
                    f"quarantined copy left in place"
                ),
                provider="LOCAL",
            )
        # Now verify containment on the resolved target.
        resolved_target = target.resolve()
        try:
            resolved_target.relative_to(self._root)
        except ValueError as exc:
            raise StorageNotFoundError(
                quarantine_id,
                provider="LOCAL",
            ) from exc
        if target.exists():
            raise StorageConflictError(
                detail=(
                    f"Cannot restore: original path is occupied by "
                    f"{provider_file_id!r}; quarantined copy left in place"
                ),
                provider="LOCAL",
            )

        try:
            # Cross-platform no-overwrite restore: use os.link + unlink.
            # This fails with FileExistsError if target exists on all platforms.
            try:
                os.link(quarantined_file, target)
                quarantined_file.unlink()
            except FileExistsError as exc:
                raise StorageConflictError(
                    detail=(
                        f"Cannot restore: original path is occupied by "
                        f"{provider_file_id!r}; quarantined copy left in place"
                    ),
                    provider="LOCAL",
                ) from exc
            except PermissionError as exc:
                # Hard-link publication denied by filesystem permissions.
                raise StoragePermissionError(
                    detail="Permission denied during restore publication",
                    provider="LOCAL",
                ) from exc
            except OSError as exc:
                # Hard-link publication unsupported (e.g. some network filesystems).
                # Do NOT fall back to rename() because it can overwrite on POSIX.
                # Preserve quarantined file and raise a normalized error.
                raise StorageUnavailableError(
                    detail=(
                        "Atomic no-overwrite restore not supported on this filesystem. "
                        "Quarantined file preserved; manual recovery required."
                    ),
                    provider="LOCAL",
                ) from exc
        except PermissionError as exc:
            raise StoragePermissionError(provider="LOCAL") from exc
        except OSError as exc:
            raise StorageUnavailableError(
                detail=f"Failed to restore file: {exc.strerror or 'unknown error'}",
                provider="LOCAL",
            ) from exc

        # Best-effort removal of the now-empty entry; the file itself is
        # already safe back at its original location.
        shutil.rmtree(entry_dir, ignore_errors=True)

        record = self._describe(target, provider_file_id)
        if meta is not None and meta.get("size_bytes") is not None:
            record = dataclasses.replace(record, size_bytes=meta["size_bytes"])
        return record

    def cleanup(self, quarantine_id: str) -> None:
        """Permanently remove a quarantined file and its metadata.

        The quarantine entry must be an actual directory, not a symlink.
        Cleanup never follows symlinks.

        Raises
        ------
        StorageNotFoundError
            If the quarantine entry does not exist or is a symlink.
        StoragePermissionError / StorageUnavailableError
            On filesystem failures during removal.
        """
        entry_dir = self._resolve_quarantine_entry(quarantine_id)
        # Re-verify after resolution: a race could replace the entry with a
        # symlink between _resolve_quarantine_entry and here. Fail closed.
        try:
            st = entry_dir.lstat()
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise StorageNotFoundError(quarantine_id, provider="LOCAL")
        except FileNotFoundError as exc:
            raise StorageNotFoundError(quarantine_id, provider="LOCAL") from exc
        except PermissionError as exc:
            raise StoragePermissionError(
                detail="Permission denied inspecting quarantine entry",
                provider="LOCAL",
            ) from exc
        except OSError as exc:
            raise StorageUnavailableError(
                detail=f"Failed to inspect quarantine entry: {exc.strerror or 'unknown error'}",
                provider="LOCAL",
            ) from exc
        try:
            shutil.rmtree(entry_dir)
        except PermissionError as exc:
            raise StoragePermissionError(provider="LOCAL") from exc
        except OSError as exc:
            raise StorageUnavailableError(
                detail=f"Failed to remove quarantine entry: {exc.strerror or 'unknown error'}",
                provider="LOCAL",
            ) from exc

    # -----------------------------------------------------------------------
    # Quarantine failure-recovery helpers
    # -----------------------------------------------------------------------

    def _write_meta_atomic(self, entry_dir: Path, meta: dict) -> None:
        """Write the quarantine sidecar atomically (temp + fsync + rename)."""
        meta_path = entry_dir / _QUARANTINE_META_FILENAME
        temp_path = entry_dir / f".{_QUARANTINE_META_FILENAME}.tmp"
        with temp_path.open("wb") as handle:
            handle.write(json.dumps(meta, indent=2).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(meta_path)

    @staticmethod
    def _remove_temp_safely(temp_path: Path) -> None:
        """Remove a leftover temporary file without masking the caller's error."""
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _remove_empty_dir(entry_dir: Path) -> None:
        """Best-effort removal of an empty directory created during quarantine."""
        try:
            entry_dir.rmdir()
        except OSError:
            pass

    @staticmethod
    def _restore_after_failed_meta(
        quarantined_file: Path, original_path: Path, entry_dir: Path
    ) -> None:
        """Put the file back after the quarantine metadata write failed.

        Never overwrites an existing file at the original path, including
        dangling symlinks. Uses atomic no-overwrite publication (os.link)
        so a race cannot overwrite a destination that appears between the
        check and the link. If safe restoration cannot be completed, the
        quarantined file is left in place and the entry directory is
        cleaned up on a best-effort basis.
        """
        # Check for symlink at the lexical target FIRST, before resolving.
        # This catches dangling symlinks (which exists() returns False for)
        # and prevents following any symlink that points outside the root.
        if original_path.is_symlink():
            # Original path is a symlink (dangling or not); leave quarantined
            # copy in place. It remains recoverable via restore().
            pass
        else:
            # Now verify containment on the resolved target.
            try:
                resolved_target = original_path.resolve()
                resolved_target.relative_to(entry_dir.parent.parent)  # storage root
            except (ValueError, OSError):
                # Path escapes storage root or cannot be resolved; leave
                # quarantined copy in place.
                pass
            else:
                # Cross-platform no-overwrite restore: use os.link + unlink.
                # This fails with FileExistsError if target exists on all platforms.
                try:
                    os.link(quarantined_file, original_path)
                    quarantined_file.unlink()
                except FileExistsError:
                    # Destination appeared between check and link; leave
                    # quarantined copy in place.
                    pass
                except (OSError, PermissionError):
                    # Hard-link publication unsupported or denied. Do NOT fall
                    # back to rename() because it can overwrite on POSIX.
                    # Preserve quarantined file and leave it in place.
                    pass

        # Best-effort cleanup of any temp meta file and the entry directory.
        try:
            temp_meta = entry_dir / f".{_QUARANTINE_META_FILENAME}.tmp"
            temp_meta.unlink(missing_ok=True)
        except OSError:
            pass
        LocalAdapter._remove_empty_dir(entry_dir)
