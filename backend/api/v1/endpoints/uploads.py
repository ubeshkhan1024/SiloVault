"""File upload and intake endpoints.

Real implementation for local file uploads. Stores files on disk and
registers metadata in the SQLite database.
"""

import hashlib
import mimetypes
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import get_db
from backend.models.unified_file import UnifiedFileRecord
from backend.schemas import FileMetadata

router = APIRouter(prefix="/uploads", tags=["uploads"])


def _get_uploads_dir() -> Path:
    """Return the configured uploads directory, creating it if necessary."""
    uploads_path = Path(settings.uploads_dir).resolve()
    uploads_path.mkdir(parents=True, exist_ok=True)
    return uploads_path


def _generate_storage_filename(original_filename: str) -> str:
    """Generate a unique, server-controlled storage filename.

    Uses a UUID prefix to ensure uniqueness while preserving the original
    extension for MIME type detection.
    """
    ext = Path(original_filename).suffix
    if ext and len(ext) > 50:  # Sanity check on extension length
        ext = ""
    return f"{uuid.uuid4()}{ext}"


def _detect_mime_type(filename: str, fallback: str = "application/octet-stream") -> str:
    """Safely detect MIME type from filename extension."""
    mime_type, _ = mimetypes.guess_type(filename)
    return mime_type or fallback


@router.post("", response_model=FileMetadata)
async def create_upload(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> FileMetadata:
    """Upload a file to local storage and register its metadata.

    - Stores the file under the configured uploads directory with a unique
      server-generated filename.
    - Calculates SHA-256 from the uploaded bytes while writing.
    - Records file size, MIME type, and original filename in the database.
    - Returns the registered file metadata.

    Security:
    - Never uses the client-provided filename as a filesystem path.
    - Prevents path traversal by using only server-generated filenames.
    - Enforces a configurable maximum upload size.
    - Cleans up partially written files if database registration fails.
    - Does not expose absolute local filesystem paths in the response.
    """
    uploads_dir = _get_uploads_dir()
    storage_filename = _generate_storage_filename(file.filename or "upload")
    storage_path = uploads_dir / storage_filename

    # Ensure we don't overwrite an existing file (extremely unlikely with UUID)
    if storage_path.exists():
        raise HTTPException(
            status_code=500,
            detail="Storage filename collision; please retry.",
        )

    sha256_hash = hashlib.sha256()
    total_size = 0
    max_size = settings.max_upload_size_bytes

    try:
        # Write file to disk while calculating hash and size
        with storage_path.open("wb") as out_file:
            while chunk := await file.read(1024 * 1024):  # 1 MiB chunks
                total_size += len(chunk)
                if total_size > max_size:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File size exceeds maximum allowed ({max_size} bytes).",
                    )
                sha256_hash.update(chunk)
                out_file.write(chunk)
    except HTTPException:
        # Clean up on size limit or other HTTP errors during write
        if storage_path.exists():
            storage_path.unlink(missing_ok=True)
        raise
    except Exception:
        # Clean up on any other error during write
        if storage_path.exists():
            storage_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to write uploaded file.",
        )

    # Finalize metadata
    sha256_hex = sha256_hash.hexdigest()
    mime_type = _detect_mime_type(file.filename or storage_filename)
    original_name = file.filename or "unknown"
    logical_path = f"/{original_name}"

    # Register in database
    record = UnifiedFileRecord(
        provider="LOCAL",
        provider_account_id=None,
        provider_file_id=storage_filename,
        name=original_name,
        logical_path=logical_path,
        mime_type=mime_type,
        size_bytes=total_size,
        sha256=sha256_hex,
        sync_state="LOCAL",
    )

    try:
        db.add(record)
        db.commit()
        db.refresh(record)
    except Exception:
        # Clean up stored file if database registration fails
        if storage_path.exists():
            storage_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to register file metadata; uploaded file cleaned up.",
        )

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