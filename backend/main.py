from contextlib import asynccontextmanager
import logging
import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.v1.router import api_router
from backend.api.v1.endpoints.google_drive import _set_adapter as _set_gdrive_adapter
from backend.config import settings
from backend.database import check_database_migrated, SessionLocal
from backend.services.recovery import RecoveryService
from backend.storage.capabilities import CAPABILITY_MAP
from backend.storage.credentials import (
    CredentialStorageError,
    load_google_drive_credentials,
)
from backend.storage.google_drive import GoogleDriveAdapter
from backend.storage.local import LocalAdapter
from backend.storage.registry import register, remove

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: gate the database on its migration state and register storage adapters.

    This intentionally does **not** call ``Base.metadata.create_all()``.
    Schema changes are applied exclusively through Alembic migration scripts.
    If the database has never been migrated (no ``alembic_version`` table),
    startup fails with an actionable error message telling the operator to
    run ``alembic upgrade head`` first.

    Google Drive adapter registration is failure-safe: if credentials are
    missing, invalid, or the keyring is unavailable, the LOCAL adapter
    continues to operate normally.

    Startup recovery runs a single batch immediately so crash survivors
    are cleaned up before the first request; a background worker then
    continues retrying any records that were waiting their scheduled
    back-off time.
    """
    check_database_migrated()

    # Register LOCAL storage adapter (always available)
    adapter = LocalAdapter(settings.uploads_dir)
    register("LOCAL", adapter, CAPABILITY_MAP["LOCAL"])

    # Attempt to restore Google Drive connection from stored credentials
    # This is best-effort — failures are logged but do not block startup.
    try:
        stored = load_google_drive_credentials()
    except CredentialStorageError as exc:
        logger.warning(
            "Google Drive credential store unavailable; skipping auto-connect: %s", exc
        )
        stored = None

    if stored:
        try:
            gdrive_adapter = GoogleDriveAdapter()
            gdrive_adapter.authenticate(
                {
                    "refresh_token": stored.refresh_token,
                    "client_id": stored.client_id,
                    "client_secret": stored.client_secret,
                    "scopes": stored.scopes,
                }
            )
            register("GOOGLE_DRIVE", gdrive_adapter, CAPABILITY_MAP["GOOGLE_DRIVE"])
            _set_gdrive_adapter(gdrive_adapter)
            logger.info("Google Drive adapter registered from stored credentials")
        except Exception as exc:
            logger.warning(
                "Failed to restore Google Drive connection from stored credentials: %s", exc
            )
            # Do not register the adapter; user can reconnect via /connect

    # Build the recovery service before yielding so it can be accessed
    # by both the one-shot startup pass and the background worker.
    recovery = RecoveryService(lambda: SessionLocal())

    # One-shot startup pass: process any records eligible right now.
    try:
        count = recovery.process_pending_deletions(batch_size=5)
        logger.info("Startup recovery processed %s pending deletion(s)", count)
    except Exception as exc:
        logger.warning("Startup recovery failed: %s", exc)

    # Launch the background polling worker as a fire-and-forget task.
    # It will keep retrying records whose next_retry_at is in the future
    # without blocking the ASGI event loop.
    app.state.recovery_service = recovery
    worker_task = asyncio.create_task(_recovery_poll_loop(recovery))
    app.state._recovery_worker_task = worker_task

    try:
        yield
    finally:
        # Stop the background worker gracefully on shutdown.
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        # Clean up adapter registrations
        remove("LOCAL")
        try:
            remove("GOOGLE_DRIVE")
        except Exception:
            pass  # Was never registered


async def _recovery_poll_loop(recovery: RecoveryService) -> None:
    """Continuously poll for and process eligible pending-deletion records.

    Runs indefinitely until cancelled. Each iteration waits at least
    ``POLL_INTERVAL_SECONDS`` between attempts so the DB is not hammered
    when nothing needs attention, and respects each record's
    ``next_retry_at`` timestamp so transient failures are retried with
    exponential backoff rather than spamming the provider.
    """
    from datetime import timedelta as _td

    POLL_INTERVAL_SECONDS = 30

    while True:
        try:
            count = recovery.process_pending_deletions(batch_size=5)
            if count > 0:
                logger.debug("Background recovery processed %s pending deletion(s)", count)
        except Exception as exc:
            logger.warning("Background recovery loop error: %s", exc)

        try:
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _sleep_seconds, POLL_INTERVAL_SECONDS),
                timeout=POLL_INTERVAL_SECONDS + 1,
            )
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            break


def _sleep_seconds(seconds: int) -> None:
    """Block for *seconds* seconds in a thread pool (for use with run_in_executor)."""
    import time
    time.sleep(seconds)


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