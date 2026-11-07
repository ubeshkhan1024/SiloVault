"""File upload and intake endpoints.

PLACEHOLDER — These routes are defined for future implementation.
No upload ingestion or mobile inbox intake logic exists yet.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/uploads", tags=["uploads"])


@router.post("")
async def create_upload():
    """Register a new file upload intake.

    TODO: Implement upload registration against the sync intake queue.
    """
    return {
        "status": "placeholder",
        "message": "Upload registration is not yet implemented.",
    }


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