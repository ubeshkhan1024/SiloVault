"""Synchronization and cloud intake endpoints.

PLACEHOLDER — These routes are defined for future implementation.
No Google Drive polling or Mobile Inbox sync service exists yet.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/sync", tags=["sync"])


@router.get("/status")
async def sync_status():
    """Return the current state of the local sync service.

    TODO: Implement sync status tracking against the sync worker state.
    """
    return {
        "status": "placeholder",
        "message": "Sync status is not yet implemented.",
        "sync_service": "not_started",
        "pending_files": 0,
        "last_sync_at": None,
    }


@router.post("/trigger")
async def trigger_sync():
    """Trigger an immediate poll of the Google Drive Mobile Inbox.

    TODO: Implement sync trigger that invokes the local sync worker.
    """
    return {
        "status": "placeholder",
        "message": "Sync trigger is not yet implemented.",
    }