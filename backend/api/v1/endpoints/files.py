"""File indexing and metadata endpoints.

Real implementation backed by the SQLite metadata database using
UnifiedFileRecord model.
"""

import logging
import mimetypes
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import get_db
from backend.models.unified_file import UnifiedFileRecord
from backend.schemas import FileListResponse, FileMetadata

router = APIRouter(prefix="/files", tags=["files"])


def _resolve_uploads_dir() -> Path:
    """Return the configured uploads directory as an absolute resolved path."""
    return Path(settings.uploads_dir).resolve()


def _safe_download_path(uploads_dir: Path, provider_file_id: str) -> Path | None:
    """Resolve and validate a download path is strictly inside uploads_dir.

    Returns the validated Path or None if the path would escape the uploads
    directory (e.g. due to a crafted ``provider_file_id`` containing '..').
    """
    candidate = (uploads_dir / provider_file_id).resolve()
    try:
        candidate.relative_to(uploads_dir)
    except ValueError:
        return None
    return candidate


@router.get("", response_model=FileListResponse)
async def list_files(
    provider: Optional[str] = Query(None, description="Filter by storage provider"),
    category: Optional[str] = Query(None, description="Filter by file category"),
    sync_state: Optional[str] = Query(None, description="Filter by sync state"),
    limit: int = Query(50, ge=1, le=100, description="Maximum number of items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    db: Session = Depends(get_db),
) -> FileListResponse:
    """List indexed files with pagination and optional filters.

    Returns a paginated list of file metadata records. Supports filtering by
    provider, category, and sync_state. Results are ordered by created_at DESC
    with a stable secondary sort by id.
    """
    # Build the base query
    stmt = select(UnifiedFileRecord)
    count_stmt = select(func.count(UnifiedFileRecord.id))

    # Apply filters
    if provider:
        stmt = stmt.where(UnifiedFileRecord.provider == provider)
        count_stmt = count_stmt.where(UnifiedFileRecord.provider == provider)
    if category:
        stmt = stmt.where(UnifiedFileRecord.category == category)
        count_stmt = count_stmt.where(UnifiedFileRecord.category == category)
    if sync_state:
        stmt = stmt.where(UnifiedFileRecord.sync_state == sync_state)
        count_stmt = count_stmt.where(UnifiedFileRecord.sync_state == sync_state)

    # Apply ordering: created_at DESC, then id DESC for stable ordering
    stmt = stmt.order_by(
        UnifiedFileRecord.created_at.desc(),
        UnifiedFileRecord.id.desc(),
    )

    # Apply pagination
    stmt = stmt.limit(limit).offset(offset)

    # Execute queries
    items = db.execute(stmt).scalars().all()
    total = db.execute(count_stmt).scalar_one()

    # Convert to response schema
    return FileListResponse(
        items=[FileMetadata.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{file_id}", response_model=FileMetadata)
async def get_file(file_id: str, db: Session = Depends(get_db)) -> FileMetadata:
    """Retrieve a single file's unified metadata by internal ID.

    Returns the full ``FileMetadata`` record for the given internal ID
    (``file_<UUID>``). Raises HTTP 404 if the record does not exist.
    """
    record = db.get(UnifiedFileRecord, file_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"File with id '{file_id}' was not found.",
        )
    return FileMetadata.model_validate(record)


@router.get("/{file_id}/download")
async def download_file(file_id: str, db: Session = Depends(get_db)):
    """Download a locally stored file by its internal record ID.

    Security guarantees:
    - Only records with ``provider='LOCAL'`` can be downloaded.
    - The on-disk filename is taken exclusively from
      ``record.provider_file_id`` (a server-generated UUID); the original
      client-supplied ``name`` is used only as the download Content-Disposition
      header value.
    - The resolved filesystem path is validated to remain strictly inside the
      configured ``uploads_dir`` before the file is served.
    - Missing records return 404; missing physical files return 404.
    """
    record = db.get(UnifiedFileRecord, file_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"File with id '{file_id}' was not found.",
        )

    if record.provider != "LOCAL":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Downloads are only supported for LOCAL provider records. "
                f"Provider '{record.provider}' is not eligible."
            ),
        )

    uploads_dir = _resolve_uploads_dir()
    safe_path = _safe_download_path(uploads_dir, record.provider_file_id)

    if safe_path is None:
        raise HTTPException(
            status_code=400,
            detail="Stored file path is invalid or outside the upload directory.",
        )

    if not safe_path.exists() or not safe_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="The requested file is no longer available on disk.",
        )

    # Use the stored MIME type when present; otherwise let the OS detect it.
    mime_type = record.mime_type or "application/octet-stream"

    # Sanitise the original name for the Content-Disposition header by stripping
    # any newline/carriage-return characters that could inject headers.
    safe_filename = "".join(ch for ch in record.name if ch not in "\r\n") or "download"

    return FileResponse(
        path=str(safe_path),
        filename=safe_filename,
        media_type=mime_type,
    )


@router.delete("/{file_id}", status_code=204)
async def delete_file(file_id: str, db: Session = Depends(get_db)):
    """Delete a locally stored file and its metadata record.

    Security guarantees:
    - Only records with ``provider='LOCAL'`` can be deleted.
    - The physical path is derived exclusively from
      ``record.provider_file_id`` (a server-generated UUID filename).
    - The resolved path must be strictly inside the configured
      ``uploads_dir``; traversal, absolute paths, and symlink escapes are
      rejected.
    - A same-directory quarantine strategy ensures the original file is never
      lost if the database commit fails: the file is first renamed to a unique
      temporary name inside ``uploads_dir``, then the DB record is deleted and
      committed. On success the quarantine file is removed; on failure the file
      is restored to its original name and the DB transaction is rolled back.
    - Quarantine files have no DB record and are therefore invisible through
      normal API endpoints.

    Returns:
    - 204 No Content on success.
    - 404 if the record does not exist or the physical file is missing.
    - 400 for non-LOCAL providers or invalid paths.
    """
    record = db.get(UnifiedFileRecord, file_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"File with id '{file_id}' was not found.",
        )

    if record.provider != "LOCAL":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Deletion is only supported for LOCAL provider records. "
                f"Provider '{record.provider}' is not eligible."
            ),
        )

    uploads_dir = _resolve_uploads_dir()
    safe_path = _safe_download_path(uploads_dir, record.provider_file_id)

    if safe_path is None:
        raise HTTPException(
            status_code=400,
            detail="Stored file path is invalid or outside the upload directory.",
        )

    # Reject symlink escapes.
    if safe_path.is_symlink():
        raise HTTPException(
            status_code=400,
            detail="Stored file path is a symlink and cannot be deleted.",
        )

    if not safe_path.exists() or not safe_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="The physical file is no longer available on disk.",
        )

    # --- Quarantine: rename file before touching the DB -------------------
    quarantine_name = f".{record.provider_file_id}.quarantined.{uuid.uuid4().hex}"
    quar_path = (uploads_dir / quarantine_name).resolve()

    # Sanity-check the quarantine path stays inside uploads_dir.
    try:
        quar_path.relative_to(uploads_dir)
    except ValueError:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Internal error: could not generate a safe quarantine path.",
        )

    try:
        safe_path.rename(quar_path)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to stage file for deletion: {exc.strerror or 'unknown error'}",
        )

    # --- Delete database record ------------------------------------------
    try:
        db.delete(record)
        db.commit()
    except Exception:
        db.rollback()
        # Restore the original file; never overwrite an existing file.
        if quar_path.exists():
            try:
                if not safe_path.exists():
                    quar_path.rename(safe_path)
                else:
                    # Collision – leave quarantine named differently.
                    logging.warning(
                        "delete_file: collision restoring %s; leaving quarantine at %s",
                        safe_path,
                        quar_path,
                    )
            except OSError as exc:
                logging.error(
                    "delete_file: failed to restore original file after DB rollback: %s",
                    exc,
                )
        raise HTTPException(
            status_code=500,
            detail="Failed to delete the metadata record; the file has been restored.",
        )

    # Commit succeeded — remove the quarantined file.
    try:
        quar_path.unlink(missing_ok=True)
    except OSError:
        # Best-effort cleanup; the file is already gone from the DB.
        logging.warning(
            "delete_file: failed to remove quarantine file %s", quar_path
        )

    return None  # 204 No Content
