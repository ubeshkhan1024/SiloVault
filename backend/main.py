"""SiloVault Backend Application Entry Point.

Initializes the FastAPI application, attaches the versioned API router, and
exposes the automatic OpenAPI documentation at `/docs`.

Database ownership
-------------------
The application does **not** run ``Base.metadata.create_all()`` at startup.
Schema versioning is owned by Alembic — migrations are explicit, reviewed
scripts committed to ``alembic/versions/``. On startup, :func:`init_db`
(renamed to :func:`check_database_migrated`) verifies that the database has
been migrated; if the ``alembic_version`` table is absent, the process fails
fast with a clear instruction to run ``alembic upgrade head``.

Never edit or delete the migration scripts without first reviewing their
impact on existing user data.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.v1.router import api_router
from backend.config import settings
from backend.database import check_database_migrated
from backend.storage.local import LocalAdapter
from backend.storage.registry import register, remove
from backend.storage.capabilities import CAPABILITY_MAP


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: gate the database on its migration state and register storage adapters.

    This intentionally does **not** call ``Base.metadata.create_all()``.
    Schema changes are applied exclusively through Alembic migration scripts.
    If the database has never been migrated (no ``alembic_version`` table),
    startup fails with an actionable error message telling the operator to
    run ``alembic upgrade head`` first.
    """
    check_database_migrated()

    # Register LOCAL storage adapter
    adapter = LocalAdapter(settings.uploads_dir)
    register("LOCAL", adapter, CAPABILITY_MAP["LOCAL"])

    try:
        yield
    finally:
        # Clean up adapter registration on shutdown
        remove("LOCAL")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "SiloVault local backend API. Privacy-focused, AI-powered cloud file "
        "management. See `/docs` for the interactive OpenAPI specification."
    ),
    lifespan=lifespan,
)

# Development CORS: allow frontend (localhost:3000) to call backend (127.0.0.1:8000)
# Narrowly scoped to known development origins only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
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