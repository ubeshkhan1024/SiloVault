"""File upload and intake endpoints.

Real implementation for local file uploads. Stores files on disk and
registers metadata in the SQLite database.
"""

import hashlib
import logging
import mimetypes

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import get_db
from backend.models.unified_file import UnifiedFileRecord
from backend.schemas import FileMetadata
from backend.services.activity import ACTION_UPLOAD, record_activity
from backend.storage.registry import lookup
from backend.storage.errors import (
    StorageConflictError,
    StorageError,
    StoragePermissionError,
    StorageQuotaExceededError,
    StorageUnavailableError,
)

router = APIRouter(prefix="/uploads", tags=["uploads"])


def _detect_mime_type(filename: str, fallback: str = "application/octet-stream") -> str:
    """Safely detect MIME type from filename extension."""
    mime_type, _ = mimetypes.guess_type(filename)
    return mime_type or fallback


@router.post("", response_model=FileMetadata)
async def create_upload(
    file: UploadFile = File(...),
    provider: str = Query("LOCAL", pattern="^[A-Z][A-Z0-9_]*$"),
    db: Session = Depends(get_db),
) -> FileMetadata:
    """Upload a file to the specified storage provider and register its metadata.

    - Stores the file under the configured uploads directory with a unique
      server-generated filename.
    - Calculates SHA-256 from the uploaded bytes while writing.
    - Records file size, MIME type, and original filename in the database.
    - Returns the registered file metadata.

    Parameters
    ----------
    provider : str
        Storage provider identifier (e.g. "LOCAL", "GOOGLE_DRIVE").
        Defaults to "LOCAL". Must match canonical provider ID pattern.

    Security:
    - Never uses the client-provided filename as a filesystem path.
    - Prevents path traversal by using only server-generated filenames.
    - Enforces a configurable maximum upload size.
    - Cleans up partially written files if database registration fails.
    - Does not expose absolute local filesystem paths in the response.
    """
    # Read file content and calculate SHA-256 + size
    content = await file.read()
    total_size = len(content)
    max_size = settings.max_upload_size_bytes
    if total_size > max_size:
        raise HTTPException(
            status_code=413,
            detail=f"File size exceeds maximum allowed ({max_size} bytes).",
        )

    sha256_hex = hashlib.sha256(content).hexdigest()
    mime_type = _detect_mime_type(file.filename or "upload")
    original_name = file.filename or "unknown"
    logical_path = f"/{original_name}"

    # Get adapter from registry and upload
    try:
        adapter, _ = lookup(provider)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Storage provider unavailable: {exc}",
        ) from exc

    try:
        provider_record = adapter.upload(content, original_name, logical_path)
    except StorageConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StorageQuotaExceededError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except StoragePermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Register in database using provider_record fields
    record = UnifiedFileRecord(
        provider=provider,
        provider_account_id=None,
        provider_file_id=provider_record.provider_file_id,
        name=provider_record.name,
        logical_path=provider_record.logical_path,
        mime_type=provider_record.mime_type,
        size_bytes=provider_record.size_bytes,
        sha256=sha256_hex,
        sync_state="LOCAL" if provider == "LOCAL" else "PENDING",
    )

    # Phase 1: Database registration commit boundary
    try:
        db.add(record)
        db.commit()
    except Exception as db_exc:
        db.rollback()
        # Use adapter quarantine/cleanup protocol to safely remove the uploaded file
        # Only LOCAL adapter implements quarantine/cleanup at this stage
        if provider == "LOCAL":
            quarantine_id = None
            try:
                adapter, _ = lookup("LOCAL")
                quarantine_id = adapter.quarantine(provider_record.provider_file_id)
                adapter.cleanup(quarantine_id)
            except Exception as cleanup_exc:
                # Log the cleanup failure but do not hide the original database failure
                logging.error(
                    "create_upload: failed to quarantine/cleanup file %s after DB failure: %s",
                    provider_record.provider_file_id,
                    cleanup_exc,
                )
        raise HTTPException(
            status_code=500,
            detail="Failed to register file metadata; uploaded file cleanup attempted.",
        ) from db_exc

    # Phase 2: Post-commit refresh (separate boundary)
    # A failure here must NOT trigger quarantine/cleanup since the commit succeeded.
    try:
        db.refresh(record)
    except Exception as refresh_exc:
        # The record is committed; do not quarantine the uploaded file.
        # Log the refresh failure and raise a 500 without cleanup.
        logging.error(
            "create_upload: db.refresh failed after successful commit for file %s: %s",
            provider_record.provider_file_id,
            refresh_exc,
        )
        raise HTTPException(
            status_code=500,
            detail="File registered but metadata refresh failed.",
        ) from refresh_exc

    # --- Activity recording: only after the file and DB record are committed.
    # A failed operation never reaches this point, so no misleading success
    # record is written.
    try:
        record_activity(
            db,
            ACTION_UPLOAD,
            file_name=original_name,
            provider=provider,
            file_id=record.id,
            details={
                "mime_type": mime_type,
                "size_bytes": total_size,
                "sha256": sha256_hex,
                "storage_filename": provider_record.provider_file_id,
            },
        )
    except Exception:
        # Activity logging is best-effort and must never break the upload
        # response that has already succeeded. Roll back only the activity
        # session state; the file record remains committed.
        db.rollback()

    return FileMetadata.model_validate(record)


@router.get("")
async def list_uploads():
    """List tracked uploads and their current intake states.

    TODO: Implement upload list query against the SQLite metadata database.
    """
    return {
        "status": "placeholder",
        "message": "Upload listing is not yet implemented.",
        "uploads": [],
    }