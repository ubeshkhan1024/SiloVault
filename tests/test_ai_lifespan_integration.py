"""End-to-end integration tests for the AI worker inside the real lifespan.

These tests execute ``app.router.lifespan_context(app)`` (the exact context
FastAPI/Starlette uses for startup and shutdown) against an isolated temporary
SQLite database.  They verify that the shared model services, the worker loop,
processor registration, task processing, lease recovery, and shutdown behave as
one integrated system.

Synchronization is event- and condition-based: processors, recovery calls and
shutdown set :class:`threading.Event` objects, and database transitions are
awaited with a bounded condition poll instead of fixed sleeps.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.database as database_module
import backend.main as main_module
import backend.models  # noqa: F401  # registers every table on Base.metadata
from backend.ai import bootstrap as bootstrap_module
from backend.ai import lifecycle, registration
from backend.ai.bootstrap import AiModelServicesUnavailableError
from backend.config import settings as app_settings
from backend.database import Base
from backend.main import app
from backend.models.ai_task import AiTask
from backend.services import ai_worker as ai_worker_module
from backend.storage.registry import remove


# ---------------------------------------------------------------------------
# Isolated application environment
# ---------------------------------------------------------------------------


@pytest.fixture
def lifespan_env(tmp_path, monkeypatch):
    """Route every lifespan dependency at a temporary database and directories."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'integration.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE alembic_version "
            "(version_num VARCHAR(32) PRIMARY KEY)"
        )
        conn.exec_driver_sql(
            "INSERT INTO alembic_version VALUES ('integration-test')"
        )
    TestSession = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, future=True
    )

    # The migration gate reads backend.database.engine at call time.
    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", TestSession)
    # The lifespan's own recovery service captures the name at call time.
    monkeypatch.setattr(main_module, "SessionLocal", TestSession)
    # The AI worker lifecycle captures its default factory in its module.
    monkeypatch.setattr(lifecycle, "SessionLocal", TestSession)

    monkeypatch.setattr(app_settings, "uploads_dir", str(tmp_path / "uploads"))
    monkeypatch.setattr(app_settings, "models_dir", str(tmp_path / "models"))
    monkeypatch.setattr(app_settings, "ai_worker_poll_interval_seconds", 0.02)
    monkeypatch.setattr(app_settings, "ai_task_recovery_interval_seconds", 60.0)

    # Startup must never read the OS keyring or contact a cloud provider.
    monkeypatch.setattr(
        main_module, "load_google_drive_credentials", lambda: None
    )

    for provider in ("LOCAL", "GOOGLE_DRIVE"):
        with contextlib.suppress(Exception):
            remove(provider)

    yield {"Session": TestSession, "engine": engine, "tmp_path": tmp_path}

    for provider in ("LOCAL", "GOOGLE_DRIVE"):
        with contextlib.suppress(Exception):
            remove(provider)
    engine.dispose()


def _lifespan():
    """The real FastAPI application lifespan context."""
    return app.router.lifespan_context(app)


async def _health_status() -> int:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/health")
        return response.status_code


# ---------------------------------------------------------------------------
# Task seeding and deterministic waiting
# ---------------------------------------------------------------------------


def _seed_pending(Session, task_type: str = "text_classification") -> str:
    task = AiTask(task_type=task_type, status="PENDING")
    db = Session()
    try:
        db.add(task)
        db.commit()
        return task.id
    finally:
        db.close()


def _seed_expired(Session, task_type: str = "text_classification") -> str:
    task = AiTask(
        task_type=task_type,
        status="PROCESSING",
        claim_token="stale-token",
        worker_id="crashed-worker",
        started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=30),
        retries=1,
        max_retries=3,
    )
    db = Session()
    try:
        db.add(task)
        db.commit()
        return task.id
    finally:
        db.close()


def _seed_active_lease(Session, task_type: str = "text_classification") -> str:
    lease = datetime.now(timezone.utc) + timedelta(minutes=5)
    task = AiTask(
        task_type=task_type,
        status="PROCESSING",
        claim_token="live-token",
        worker_id="live-worker",
        started_at=datetime.now(timezone.utc),
        lease_expires_at=lease,
        retries=0,
        max_retries=3,
    )
    db = Session()
    try:
        db.add(task)
        db.commit()
        return task.id
    finally:
        db.close()


def _get_task(Session, task_id: str) -> AiTask:
    db = Session()
    try:
        task = db.get(AiTask, task_id)
        assert task is not None
        return task
    finally:
        db.close()


async def _wait_event(event: threading.Event, timeout: float = 5.0) -> bool:
    """Wait for a thread event without blocking the event loop."""
    return await asyncio.get_running_loop().run_in_executor(
        None, event.wait, timeout
    )


async def _wait_until(predicate, timeout: float = 5.0) -> bool:
    """Bounded condition wait for a database transition."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------------------
# Test helpers that observe the integrated runtime
# ---------------------------------------------------------------------------


def _install_processors(monkeypatch, processors):
    """Inject processors into the existing registration composition path.

    ``build_ai_worker`` still runs for real; only the capability availability
    filter is replaced, so the default (empty) catalogue does not hide the
    registration and processing path under test.
    """
    monkeypatch.setattr(
        registration,
        "build_task_processors",
        lambda session_factory, services: dict(processors),
    )


def _observe_recovery(monkeypatch) -> threading.Event:
    """Wrap the worker's recovery call and signal after it completes."""
    event = threading.Event()
    original = ai_worker_module.AiWorkerService.recover_expired_tasks

    def observed(self, batch_size=ai_worker_module.DEFAULT_RECOVERY_BATCH_SIZE):
        count = original(self, batch_size=batch_size)
        event.set()
        return count

    monkeypatch.setattr(
        ai_worker_module.AiWorkerService, "recover_expired_tasks", observed
    )
    return event


def _ai_worker_tasks() -> list[asyncio.Task]:
    return [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("ai-worker-")
    ]


# ---------------------------------------------------------------------------
# Startup, services and registration
# ---------------------------------------------------------------------------


async def test_lifespan_builds_shared_services_and_one_worker(
    lifespan_env, monkeypatch
):
    builds = []
    real_build = bootstrap_module.build_ai_model_services

    def counting_build(settings=None):
        builds.append(settings)
        return real_build(settings)

    monkeypatch.setattr(
        bootstrap_module, "build_ai_model_services", counting_build
    )
    _install_processors(
        monkeypatch, {"text_classification": lambda task_id: {"ok": True}}
    )

    async with _lifespan():
        assert len(builds) == 1
        assert app.state.ai_models is not None
        assert app.state.ai_models_error is None
        assert app.state.ai_worker_error is None

        runtime = app.state.ai_worker_runtime
        assert isinstance(runtime, lifecycle.AiWorkerRuntime)
        assert app.state.ai_worker is runtime.worker
        assert runtime.is_running is True
        assert runtime.accepting_tasks is True
        assert runtime.task_types == ["text_classification"]
        assert len(_ai_worker_tasks()) == 1

        assert await _health_status() == 200

    assert runtime.is_running is False
    assert runtime.accepting_tasks is False
    assert app.state.ai_worker_runtime is None
    assert app.state.ai_worker is None
    assert _ai_worker_tasks() == []


async def test_lifespan_registers_each_provided_processor_once(
    lifespan_env, monkeypatch
):
    first = lambda task_id: {"kind": "first"}  # noqa: E731
    second = lambda task_id: {"kind": "second"}  # noqa: E731
    _install_processors(
        monkeypatch, {"text_classification": first, "tagging": second}
    )

    async with _lifespan():
        runtime = app.state.ai_worker_runtime
        worker = runtime.worker
        assert runtime.task_types == ["text_classification", "tagging"]
        assert worker.get_processor("text_classification") is first
        assert worker.get_processor("tagging") is second


# ---------------------------------------------------------------------------
# Claiming, processing and recovery in the live runtime
# ---------------------------------------------------------------------------


async def test_pending_task_is_claimed_and_processed(lifespan_env, monkeypatch):
    Session = lifespan_env["Session"]
    entered = threading.Event()
    offered = []

    def processor(task_id):
        offered.append(task_id)
        entered.set()
        return {"tags": ["receipt"]}

    _install_processors(monkeypatch, {"text_classification": processor})
    task_id = _seed_pending(Session)

    async with _lifespan():
        assert await _wait_event(entered), "worker never invoked the processor"
        assert await _wait_until(
            lambda: _get_task(Session, task_id).status == "AWAITING_APPROVAL"
        ), "task was not finalized as AWAITING_APPROVAL"

    task = _get_task(Session, task_id)
    assert offered == [task_id]
    assert task.proposal_json == {"tags": ["receipt"]}
    assert task.claim_token is None
    assert task.worker_id is None
    assert task.lease_expires_at is None
    assert task.started_at is not None
    assert task.completed_at is not None


async def test_recovery_and_processing_share_one_runtime(
    lifespan_env, monkeypatch
):
    Session = lifespan_env["Session"]
    recovered = _observe_recovery(monkeypatch)
    processed = threading.Event()

    def processor(task_id):
        processed.set()
        return {"recovered": True}

    _install_processors(monkeypatch, {"text_classification": processor})
    task_id = _seed_expired(Session)

    async with _lifespan():
        assert await _wait_event(recovered), "recovery never ran"
        assert await _wait_event(processed), "recovered task was not processed"
        assert await _wait_until(
            lambda: _get_task(Session, task_id).status == "AWAITING_APPROVAL"
        )

    task = _get_task(Session, task_id)
    assert task.retries == 2
    assert task.proposal_json == {"recovered": True}
    assert task.claim_token is None
    # Only one loop ran the whole time.
    assert len(_ai_worker_tasks()) == 0


async def test_expired_task_is_recovered_when_no_processor_exists(
    lifespan_env, monkeypatch
):
    Session = lifespan_env["Session"]
    recovered = _observe_recovery(monkeypatch)
    _install_processors(monkeypatch, {})
    task_id = _seed_expired(Session)

    async with _lifespan():
        runtime = app.state.ai_worker_runtime
        assert runtime.task_types == []
        assert await _wait_event(recovered), "recovery never ran"

        task = _get_task(Session, task_id)
        assert task.status == "PENDING"
        assert task.retries == 2
        assert task.claim_token is None
        assert task.worker_id is None
        assert task.lease_expires_at is None
        assert task.started_at is None


async def test_active_lease_is_preserved(lifespan_env, monkeypatch):
    Session = lifespan_env["Session"]
    recovered = _observe_recovery(monkeypatch)
    _install_processors(monkeypatch, {})
    task_id = _seed_active_lease(Session)
    original = _get_task(Session, task_id)
    original_lease = original.lease_expires_at

    async with _lifespan():
        assert await _wait_event(recovered), "recovery never ran"

        task = _get_task(Session, task_id)
        assert task.status == "PROCESSING"
        assert task.worker_id == "live-worker"
        assert task.claim_token == "live-token"
        assert task.lease_expires_at == original_lease
        assert task.retries == 0


# ---------------------------------------------------------------------------
# Shutdown behavior
# ---------------------------------------------------------------------------


async def test_shutdown_prevents_new_claims(lifespan_env, monkeypatch):
    Session = lifespan_env["Session"]
    _install_processors(monkeypatch, {})
    task_id = _seed_pending(Session)
    runtime = None

    async with _lifespan():
        runtime = app.state.ai_worker_runtime
        assert runtime.accepting_tasks is True

    assert runtime.accepting_tasks is False
    assert runtime.is_running is False
    assert runtime._task is None
    # The stopped runtime cannot claim anything.
    assert runtime.run_cycle() == 0
    assert _get_task(Session, task_id).status == "PENDING"


async def test_shutdown_waits_for_in_flight_task(lifespan_env, monkeypatch):
    Session = lifespan_env["Session"]
    entered = threading.Event()
    release = threading.Event()
    shutdown_started = threading.Event()

    def blocking_processor(task_id):
        entered.set()
        assert release.wait(10.0)
        return {"finished": True}

    _install_processors(monkeypatch, {"text_classification": blocking_processor})
    real_stop = main_module.stop_ai_worker

    async def observed_stop(app_arg):
        shutdown_started.set()
        return await real_stop(app_arg)

    monkeypatch.setattr(main_module, "stop_ai_worker", observed_stop)
    task_id = _seed_pending(Session)

    stack = contextlib.AsyncExitStack()
    await stack.enter_async_context(_lifespan())
    runtime = app.state.ai_worker_runtime
    assert await _wait_event(entered), "processor never started"

    exiting = asyncio.create_task(stack.aclose())
    assert await _wait_event(shutdown_started), "shutdown never started"
    # Shutdown is waiting for the in-flight cycle instead of abandoning it.
    assert exiting.done() is False
    assert runtime.accepting_tasks is False

    release.set()
    await asyncio.wait_for(exiting, timeout=10.0)

    task = _get_task(Session, task_id)
    assert task.status == "AWAITING_APPROVAL"
    assert task.proposal_json == {"finished": True}
    assert runtime.is_running is False
    assert _ai_worker_tasks() == []


# ---------------------------------------------------------------------------
# Failure handling and repeated startup
# ---------------------------------------------------------------------------


async def test_worker_startup_failure_leaks_no_background_task(
    lifespan_env, monkeypatch
):
    def exploding_build(session_factory, services):
        raise RuntimeError("cannot compose processors")

    monkeypatch.setattr(lifecycle, "build_ai_worker", exploding_build)

    async with _lifespan():
        assert app.state.ai_worker is None
        assert app.state.ai_worker_runtime is None
        assert app.state.ai_worker_error == "cannot compose processors"
        assert _ai_worker_tasks() == []
        # The rest of the application is fully operational.
        assert await _health_status() == 200

    assert _ai_worker_tasks() == []
    assert await _health_status() == 200


async def test_repeated_lifespan_does_not_duplicate_workers(
    lifespan_env, monkeypatch
):
    builds = []
    real_build = bootstrap_module.build_ai_model_services

    def counting_build(settings=None):
        builds.append(settings)
        return real_build(settings)

    monkeypatch.setattr(
        bootstrap_module, "build_ai_model_services", counting_build
    )

    started = []
    original_start = lifecycle.AiWorkerRuntime.start

    def tracking_start(self):
        created = original_start(self)
        if created:
            started.append(self)
        return created

    monkeypatch.setattr(lifecycle.AiWorkerRuntime, "start", tracking_start)

    async with _lifespan():
        first = app.state.ai_worker_runtime
        assert len(started) == 1
        assert len(_ai_worker_tasks()) == 1

    async with _lifespan():
        second = app.state.ai_worker_runtime
        assert second is not first
        assert first.is_running is False
        assert len(started) == 2
        assert [runtime for runtime in started if runtime.is_running] == [second]
        assert len(_ai_worker_tasks()) == 1
        assert len(builds) == 2  # one shared model container per startup

    assert [runtime for runtime in started if runtime.is_running] == []


async def test_shutdown_releases_background_loops_promptly(lifespan_env):
    """Regression: shutdown must not block on a non-cancellable poll sleep.

    The deletion-recovery loop previously slept in an executor thread; a
    cancelled lifespan left that thread sleeping for its full 30 s interval,
    so event-loop shutdown joined it and every shutdown took 30 s.
    """
    began = time.monotonic()
    async with _lifespan():
        assert app.state.ai_worker_runtime.is_running is True
    elapsed = time.monotonic() - began

    assert elapsed < 5.0, f"lifespan shutdown took {elapsed:.1f}s"
    assert _ai_worker_tasks() == []


async def test_missing_model_services_still_starts_application(
    lifespan_env, monkeypatch
):
    def unavailable(settings=None):
        raise AiModelServicesUnavailableError(
            "Model storage is unavailable: no writable root"
        )

    monkeypatch.setattr(
        bootstrap_module, "build_ai_model_services", unavailable
    )

    async with _lifespan():
        assert app.state.ai_models is None
        assert "Model storage is unavailable" in app.state.ai_models_error
        assert app.state.ai_worker is None
        assert app.state.ai_worker_runtime is None
        assert "Model storage is unavailable" in app.state.ai_worker_error
        assert _ai_worker_tasks() == []
        assert await _health_status() == 200

    assert _ai_worker_tasks() == []
