"""Activity history and audit log endpoints.

PLACEHOLDER — These routes are defined for future implementation.
No local operational history or rollback registry exists yet.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/activity", tags=["activity"])


@router.get("")
async def list_activity():
    """List recent operational history and audit log entries.

    TODO: Implement activity query against the SQLite audit log.
    """
    return {
        "status": "placeholder",
        "message": "Activity history is not yet implemented.",
        "entries": [],
    }


@router.post("/rollback/{activity_id}")
async def rollback_activity(activity_id: str):
    """Request rollback of a previous move or rename operation.

    TODO: Implement single-click rollback using stored operation history.
    """
    return {
        "status": "placeholder",
        "message": "Activity rollback is not yet implemented.",
        "activity_id": activity_id,
    }