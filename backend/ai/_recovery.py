"""Journal-based startup recovery for SiloVault model installations."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from backend.ai._transaction import (
    read_journal,
    write_journal,
    make_journal_record,
    _is_safe_relative_path,
    TRANSACTIONS_DIR_NAME,
)
from backend.ai._manifest_validation import _validate_v2_manifest
from backend.ai.installation import _remove_dir, _hash_file, MANIFEST_NAME
import json
import shutil
import logging

logger = logging.getLogger(__name__)


def recover_all(storage_root: Path) -> None:
    """Scan and recover all incomplete transactions in storage_root."""
    txns_dir = storage_root / TRANSACTIONS_DIR_NAME
    if not txns_dir.is_dir():
        return

    for entry in txns_dir.glob("*.json"):
        transaction_id = entry.stem
        journal = read_journal(storage_root, transaction_id=transaction_id)
        if journal is None:
            continue
        _recover_transaction(storage_root, journal)


def _recover_transaction(storage_root: Path, journal: dict) -> None:
    """Recover a single transaction."""
    state = journal.get("state")
    if state in ("committed", "rolled_back", "failed"):
        return

    transaction_id = journal["transaction_id"]
    staging_rel = journal.get("staging_dir")
    target_rel = journal.get("target_dir")
    backup_rel = journal.get("backup_dir")

    # Validate paths
    if staging_rel and not _is_safe_relative_path(staging_rel, base=storage_root):
        return
    if target_rel and not _is_safe_relative_path(target_rel, base=storage_root):
        return
    if backup_rel and not _is_safe_relative_path(backup_rel, base=storage_root):
        return

    target_dir = storage_root / target_rel if target_rel else None
    staging_dir = storage_root / staging_rel if staging_rel else None
    backup_dir = storage_root / backup_rel if backup_rel else None

    if state == "staging":
        # Never started publication. Cleanup staging.
        if staging_dir and staging_dir.exists():
            _remove_dir(staging_dir)
        _mark_transaction(storage_root, journal, "rolled_back")
        # If backup exists, restore it (if target is missing/incomplete)
        if backup_dir and backup_dir.exists() and target_dir:
            if not target_dir.exists() or not (target_dir / MANIFEST_NAME).exists():
                try:
                    if target_dir.exists():
                        _remove_dir(target_dir)
                    shutil.move(backup_dir, target_dir)
                except OSError:
                    pass

    elif state == "publishing":
        # Publication in progress.
        # Check if target is valid v2
        is_valid = False
        if target_dir and target_dir.exists():
            manifest_path = target_dir / MANIFEST_NAME
            if manifest_path.exists():
                try:
                    with manifest_path.open("r", encoding="utf-8") as f:
                        manifest = json.load(f)
                    _validate_v2_manifest(manifest)
                    
                    # Verify files
                    verification_ok = True
                    for file_info in manifest["files"]:
                        file_path = target_dir / file_info["path"]
                        if not file_path.is_file():
                            verification_ok = False
                            break
                        if file_path.stat().st_size != file_info["size"] or _hash_file(file_path, "sha256") != file_info["sha256"]:
                            verification_ok = False
                            break
                    if verification_ok:
                        is_valid = True
                except Exception:
                    pass

        if is_valid:
            _mark_transaction(storage_root, journal, "committed")
            if backup_dir and backup_dir.exists():
                _remove_dir(backup_dir)
        elif backup_dir and backup_dir.exists():
            # Rollback: restore backup to target
            try:
                if target_dir and target_dir.exists():
                    _remove_dir(target_dir)
                shutil.move(backup_dir, target_dir)
                _mark_transaction(storage_root, journal, "rolled_back")
            except OSError:
                pass # Preserve both
        else:
            # Failed with no backup.
            if target_dir and target_dir.exists():
                _remove_dir(target_dir)
            if staging_dir and staging_dir.exists():
                _remove_dir(staging_dir)
            _mark_transaction(storage_root, journal, "failed")


def _mark_transaction(storage_root: Path, journal: dict, new_state: str) -> None:
    """Update journal state."""
    journal["state"] = new_state
    try:
        write_journal(storage_root, transaction_id=journal["transaction_id"], record=journal)
    except OSError:
        pass



def _mark_transaction(storage_root: Path, journal: dict, new_state: str) -> None:
    """Update journal state."""
    journal["state"] = new_state
    try:
        write_journal(storage_root, transaction_id=journal["transaction_id"], record=journal)
    except OSError:
        pass
