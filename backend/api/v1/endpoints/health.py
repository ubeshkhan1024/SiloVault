"""Health and system status endpoints.

The `GET /api/v1/health` endpoint is the only functional route at the skeleton
stage. It confirms that the backend is running and returns basic metadata.
"""

from datetime import datetime, timezone

from fastapi import APIRouter

from backend.config import settings

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
async def health_check():
    """Return a simple JSON response indicating that the API is running.

    This is the only functional endpoint in the current skeleton. It is used
    to verify that the FastAPI application starts correctly and is reachable.
    """
    return {
        "status": "healthy",
        "app": settings.app_name,
        "version": settings.app_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


system_router = APIRouter(prefix="/system", tags=["system"])


@system_router.get("/status")
async def system_status():
    """Return local backend component status.

    PLACEHOLDER — This route is defined for future implementation.
    The backend is not yet wired to Ollama, SQLite, or the sync service.
    """
    return {
        "status": "placeholder",
        "message": "System status is not yet implemented.",
        "components": {
            "ollama": "not_connected",
            "sync_service": "not_started",
            "database": "not_initialized",
            "pending_approvals_count": 0,
        },
    }