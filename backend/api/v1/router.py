"""SiloVault API v1 router.

Aggregates all endpoint modules under the `/api/v1` prefix.
"""

from fastapi import APIRouter

from backend.api.v1.endpoints import (
    activity,
    files,
    health,
    processing,
    review,
    sync,
    uploads,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(health.system_router)
api_router.include_router(files.router)
api_router.include_router(uploads.router)
api_router.include_router(sync.router)
api_router.include_router(processing.router)
api_router.include_router(review.router)
api_router.include_router(activity.router)