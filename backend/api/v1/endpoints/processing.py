"""AI processing pipeline endpoints.

PLACEHOLDER — These routes are defined for future implementation.
No local Ollama integration, OCR, or classification pipeline exists yet.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/process/{file_id}")
async def process_file(file_id: str):
    """Trigger local AI processing for a file.

    TODO: Implement local OCR, metadata extraction, and classification.
    """
    return {
        "status": "placeholder",
        "message": "AI processing is not yet implemented.",
        "file_id": file_id,
    }
