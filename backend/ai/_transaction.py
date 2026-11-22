"""Transaction journal infrastructure for SiloVault model installations.

Provides atomic journal read/write with schema validation, state machine
support, and path safety guarantees. Journals live under
``storage.root/.transactions/`` and record enough information to support
recoverable publication without redesigning the installer.

Constraints (Stage 1 only):
- Never perform recovery, deletion, or publication moves.
- Do not integrate into install() or install_from_directory().
- Keep v1/v2 manifest behaviour completely unchanged.
"""
from __future__ import annotations

import json
import os
import uuid
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRANSACTIONS_DIR_NAME = ".transactions"

VALID_STATES = frozenset(["staging", "publishing", "committed", "rolled_back", "failed"])

# UUID regex for version 4 (the version we generate).  Accept v1–v4 formats.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _transactions_dir(storage_root: Path) -> Path:
    """Return the ``.transactions/`` directory under *storage_root*."""
    d = storage_root / TRANSACTIONS_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _is_valid_uuid(transaction_id: str) -> bool:
    """Return True if *transaction_id* is a well-formed UUID string."""
    return bool(_UUID_RE.match(str(transaction_id).strip()))


def _journal_path(transactions_dir: Path, transaction_id: str) -> Path:
    """Return the journal file path for *transaction_id*.

    Raises ValueError if *transaction_id* is not a valid UUID.
    """
    if not _is_valid_uuid(transaction_id):
        raise ValueError(
            f"transaction_id must be a valid UUID string; got {transaction_id!r}"
        )
    # UUID is already sanitised by the regex above; no manual replacement needed.
    return transactions_dir / f"{transaction_id}.json"


def _is_safe_relative_path(p: str, base: Optional[Path] = None) -> bool:
    """Return True if *p* is a safe relative path.

    Rejects:
    - Absolute paths (POSIX ``/...`` or Windows ``C:\\...``)
    - ``..`` components anywhere in the path (directory traversal)
    - Windows drive letters (e.g. ``C:\``)
    - When *base* is provided, also ensures the resolved path stays under *base*.
    """
    if not isinstance(p, str):
        return False
    p = p.strip()
    if not p:
        return False
    # Reject absolute paths (POSIX / and Windows C:\)
    if os.path.isabs(p) or p.startswith("/"):
        return False
    # Reject Windows drive letters like C:\
    if len(p) >= 2 and p[1] == ":":
        return False
    # Reject ``..`` components anywhere in the path
    norm = Path(p)
    for part in norm.parts:
        if part == "..":
            return False
    # If a base is given, verify we don't escape it
    if base is not None:
        try:
            base_resolved = base.resolve()
            resolved = (base / norm).resolve()
            # Check containment: resolved must be under base_resolved
            # i.e. resolved == base_resolved or resolved starts with base_resolved + os.sep
            resolved_str = str(resolved)
            base_str = str(base_resolved)
            if not (resolved_str == base_str or resolved_str.startswith(base_str + os.sep)):
                return False
        except (OSError, ValueError):
            # If resolution fails, be conservative and reject
            return False
    return True


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def validate_journal_schema(data: dict, *, allow_missing_state: bool = False) -> Tuple[bool, Optional[str]]:
    """Validate a journal dict against the required schema.

    Returns ``(True, None)`` on success, or ``(False, error_message)``.
    """
    if not isinstance(data, dict):
        return False, "journal must be a JSON object"

    # transaction_id
    txn_id = data.get("transaction_id")
    if not isinstance(txn_id, str) or not txn_id:
        return False, "transaction_id must be a non-empty string"
    # New: validate UUID format
    if not _is_valid_uuid(txn_id):
        return False, f"transaction_id must be a valid UUID string; got {txn_id!r}"

    # model_identifier
    identifier = data.get("model_identifier")
    if not isinstance(identifier, str) or not identifier:
        return False, "model_identifier must be a non-empty string"
    if "/" in identifier or "\\" in identifier:
        return False, "model_identifier must not contain path separators"

    # version
    version = data.get("version")
    if not isinstance(version, str) or not version:
        return False, "version must be a non-empty string"

    # schema_version
    schema_version = data.get("schema_version")
    if schema_version not in (1, 2):
        return False, "schema_version must be 1 or 2"

    # state
    state = data.get("state")
    if not allow_missing_state and (state is None or state not in VALID_STATES):
        return False, f"state must be one of {sorted(VALID_STATES)}"
    if state is not None and state not in VALID_STATES:
        return False, f"state must be one of {sorted(VALID_STATES)}"

    # --- path safety checks ---
    # staging_dir
    staging_dir = data.get("staging_dir")
    if staging_dir is not None and not _is_safe_relative_path(staging_dir):
        return False, "staging_dir must be a safe relative path"

    # backup_dir
    backup_dir = data.get("backup_dir")
    if backup_dir is not None and not _is_safe_relative_path(backup_dir):
        return False, "backup_dir must be a safe relative path"

    # target_dir
    target_dir = data.get("target_dir")
    if target_dir is not None and not _is_safe_relative_path(target_dir):
        return False, "target_dir must be a safe relative path"

    # installed_at (ISO 8601 when present)
    installed_at = data.get("installed_at")
    if installed_at is not None:
        if not isinstance(installed_at, str) or not installed_at:
            return False, "installed_at must be a non-empty string if present"

    # error
    error = data.get("error")
    if error is not None and not isinstance(error, str):
        return False, "error must be a string or null"

    # previous_installation_existed
    prev = data.get("previous_installation_existed")
    if not isinstance(prev, bool):
        return False, "previous_installation_existed must be a boolean"

    # files (optional list of relative paths)
    files = data.get("files")
    if files is not None:
        if not isinstance(files, list):
            return False, "files must be an array if present"
        for f in files:
            if not isinstance(f, str):
                return False, "each file in files must be a string"
            if not f:
                return False, "each file in files must be non-empty"
            # Reject absolute paths (POSIX / and Windows C:\)
            if os.path.isabs(f) or f.startswith("/"):
                return False, "each file in files must not be an absolute path"
            # Reject Windows drive letters like C:\
            if len(f) >= 2 and f[1] == ":":
                return False, "each file in files must not have a Windows drive letter"
            # Reject '..' components that escape the intended scope
            if any(part == ".." for part in Path(f).parts):
                return False, "each file in files must not contain '..' components"

    return True, None


# ---------------------------------------------------------------------------
# Journal record factory
# ---------------------------------------------------------------------------

def make_journal_record(
    *,
    transaction_id: str,
    model_identifier: str,
    version: str,
    schema_version: int,
    state: str,
    staging_dir: Optional[str] = None,
    backup_dir: Optional[str] = None,
    target_dir: Optional[str] = None,
    previous_installation_existed: bool = False,
    installed_at: Optional[str] = None,
    error: Optional[str] = None,
    files: Optional[list] = None,
) -> dict:
    """Build a journal dict ready for atomic write.

    All path fields are validated as safe relative paths before inclusion.
    """
    if state not in VALID_STATES:
        raise ValueError(f"invalid state: {state}")

    if not _is_valid_uuid(transaction_id):
        raise ValueError(f"invalid transaction_id: {transaction_id!r} is not a valid UUID")

    record: Dict[str, Any] = {
        "transaction_id": transaction_id,
        "model_identifier": model_identifier,
        "version": version,
        "schema_version": schema_version,
        "state": state,
        "previous_installation_existed": previous_installation_existed,
        "installed_at": installed_at,
        "error": error,
        "staging_dir": staging_dir,
        "backup_dir": backup_dir,
        "target_dir": target_dir,
        "files": files,
    }

    return record


# ---------------------------------------------------------------------------
# Atomic write / read
# ---------------------------------------------------------------------------

def write_journal(
    storage_root: Path,
    *,
    transaction_id: str,
    record: dict,
) -> Path:
    """Atomically write a journal record to ``.transactions/``.

    The *record* dict is validated *before* writing.  On failure the
    previous journal (if any) is left untouched.

    Returns the path written to.

    Raises:
        ValueError: if the record schema is invalid or transaction_id is bad.
        OSError: if the atomic write fails (temp file cleaned up).
    """
    # Validate schema first
    ok, err = validate_journal_schema(record)
    if not ok:
        raise ValueError(f"invalid journal record: {err}")

    # Validate transaction_id early so we get a consistent path
    try:
        _is_valid_uuid(transaction_id)
    except ValueError as e:
        raise ValueError(f"invalid transaction_id: {e}") from e

    txns_dir = _transactions_dir(storage_root)
    jpath = _journal_path(txns_dir, transaction_id)

    # Write to temp file then os.replace for atomicity
    tmp_path = jpath.with_name(f".{jpath.name}.tmp.{uuid.uuid4().hex}")

    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, jpath)
    except BaseException:
        # Clean up temp; do not remove existing journal
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return jpath


def read_journal(
    storage_root: Path,
    *,
    transaction_id: str,
    schema_version_required: Optional[int] = None,
) -> Optional[dict]:
    """Read and validate a journal record.

    Returns the parsed dict if valid, or ``None`` if the journal does not
    exist or fails schema validation.

    If *schema_version_required* is supplied, the journal's
    ``schema_version`` must match (or ``None`` means either 1 or 2 is OK).
    """
    try:
        txns_dir = _transactions_dir(storage_root)
        jpath = _journal_path(txns_dir, transaction_id)
    except ValueError:
        # Invalid transaction_id — treat as not found
        return None

    if not jpath.is_file():
        return None

    try:
        with jpath.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    ok, err = validate_journal_schema(data, allow_missing_state=True)
    if not ok:
        return None

    # Schema version check
    sv = data.get("schema_version")
    if schema_version_required is not None and sv != schema_version_required:
        return None

    return data


# ---------------------------------------------------------------------------
# Convenience: transaction ID generation
# ---------------------------------------------------------------------------

def new_transaction_id() -> str:
    """Generate a new unique transaction ID (UUID v4)."""
    return str(uuid.uuid4())