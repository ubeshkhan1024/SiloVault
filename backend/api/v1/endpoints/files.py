"""File indexing and metadata endpoints.

Real implementation backed by the SQLite metadata database using
UnifiedFileRecord model.
"""

import logging
import mimetypes
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import get_db
from backend.models.unified_file import UnifiedFileRecord
from backend.schemas import FileListResponse, FileMetadata
from backend.services.activity import (
    ACTION_DELETE,
    ACTION_DOWNLOAD,
    record_activity,
)
from backend.storage.registry import lookup
from backend.storage.errors import (
    StorageError,
    StorageNotFoundError,
    StoragePermissionError,
    StorageUnavailableError,
)

router = APIRouter(prefix="/files", tags=["files"])


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
    - The LOCAL adapter validates the path remains strictly inside the
      configured storage root before the file is served.
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

    # Get LOCAL adapter from registry and download
    try:
        adapter, _ = lookup("LOCAL")
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Storage provider unavailable: {exc}",
        ) from exc

    try:
        file_bytes = adapter.download(record.provider_file_id)
    except StorageNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StoragePermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Use the stored MIME type when present; otherwise let the OS detect it.
    mime_type = record.mime_type or "application/octet-stream"

    # Sanitise the original name for the Content-Disposition header by stripping
    # any newline/carriage-return characters that could inject headers.
    safe_filename = "".join(ch for ch in record.name if ch not in "\r\n") or "download"

    # --- Activity recording: recorded when the download response is
    # successfully initiated. We do NOT claim the user completed the download;
    # this records that the server successfully served the file.
    try:
        record_activity(
            db,
            ACTION_DOWNLOAD,
            file_name=record.name,
            provider=record.provider,
            file_id=record.id,
            details={"mime_type": mime_type, "size_bytes": record.size_bytes},
        )
    except Exception:
        db.rollback()

    # Return bytes directly instead of using FileResponse to avoid filesystem path exposure
    # RFC 5987 encoding: filename*=utf-8''<url-encoded-filename>
    encoded_filename = quote(safe_filename, encoding='utf-8')
    return Response(
        content=file_bytes,
        media_type=mime_type,
        headers={
            "Content-Disposition": f"attachment; filename*=utf-8''{encoded_filename}",
        },
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
    - Quarantine/restore/cleanup protocol ensures the original file is never
      lost if the database commit fails: the file is first quarantined, then
      the DB record is deleted and committed. On success the quarantine file
      is cleaned up; on failure the file is restored via the adapter.
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

    # Snapshot the original filename BEFORE the DB record is removed. The
    # delete activity must remain identifiable even after the file record is
    # gone, so we keep this snapshot rather than a foreign key.
    deleted_file_name = record.name
    deleted_provider = record.provider
    provider_file_id = record.provider_file_id

    # Get LOCAL adapter from registry
    try:
        adapter, _ = lookup("LOCAL")
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Storage provider unavailable: {exc}",
        ) from exc

    # Phase 1: Quarantine the physical file
    try:
        quarantine_id = adapter.quarantine(provider_file_id)
    except StorageNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StoragePermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Phase 2: Delete database record
    try:
        db.delete(record)
        db.commit()
    except Exception as db_exc:
        db.rollback()
        # Phase 3b: Restore the quarantined file
        try:
            adapter.restore(quarantine_id)
        except Exception as restore_exc:
            logging.error(
                "delete_file: failed to restore quarantined file %s after DB rollback: %s",
                quarantine_id,
                restore_exc,
            )
        raise HTTPException(
            status_code=500,
            detail="Failed to delete the metadata record; the file has been restored.",
        ) from db_exc

    # Phase 3a: Commit succeeded — permanently remove the quarantined file
    try:
        adapter.cleanup(quarantine_id)
    except Exception as cleanup_exc:
        # Best-effort cleanup; the file is already gone from the DB.
        # Log the failure but do not report the deletion as failed.
        logging.warning(
            "delete_file: failed to cleanup quarantine %s after successful DB commit: %s",
            quarantine_id,
            cleanup_exc,
        )

    # --- Activity recording: only after the deletion operation succeeds and
    # the file record has been removed. ``file_id`` is intentionally NULL so
    # the activity outlives the file record; ``file_name`` is the snapshot
    # taken before deletion.
    try:
        record_activity(
            db,
            ACTION_DELETE,
            file_name=deleted_file_name,
            provider=deleted_provider,
            file_id=None,
            details={"storage_filename": provider_file_id},
        )
    except Exception:
        db.rollback()

    return None  # 204 No Content