"""SiloVault Backend Application Entry Point.

Initializes the FastAPI application, attaches the versioned API router, and
exposes the automatic OpenAPI documentation at `/docs`.

This is a structural skeleton. Only the `/api/v1/health` endpoint is currently
functional. All other routes are placeholder stubs marked with `TODO`.
"""

from fastapi import FastAPI

from backend.api.v1.router import api_router
from backend.config import settings

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "SiloVault local backend API. Privacy-focused, AI-powered cloud file "
        "management. See `/docs` for the interactive OpenAPI specification."
    ),
)

# Mount the versioned API router.
app.include_router(api_router, prefix="/api/v1")


@app.get("/", include_in_schema=False)
async def root():
    """Root redirect pointing to the API health endpoint."""
    return {"message": "SiloVault backend is running. Visit /api/v1/health or /docs."}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )