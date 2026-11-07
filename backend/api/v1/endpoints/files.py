"""File indexing and metadata endpoints.

PLACEHOLDER — These routes are defined for future implementation.
No file indexing or database schema exists yet.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/files", tags=["files"])


@router.get("")
async def list_files():
    """List indexed files.

    TODO: Implement file indexing against the SQLite metadata database.
    """
    return {
        "status": "placeholder",
        "message": "File listing is not yet implemented.",
        "files": [],
    }


@router.get("/{file_id}")
async def get_file(file_id: str):
    """Retrieve file details, tags, and category metadata.

    TODO: Implement file lookup against the SQLite metadata database.
    """
    return {
        "status": "placeholder",
        "message": "File lookup is not yet implemented.",
        "file_id": file_id,
    }