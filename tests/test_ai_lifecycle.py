"""Focused tests for M4.4 Step 3D application lifespan integration."""

from __future__ import annotations

import ast
import asyncio
import pathlib
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.ai import lifecycle
from backend.ai.bootstrap import build_ai_model_services
from backend.ai.lifecycle import (
    AiWorkerRuntime,
    start_ai_worker,
    stop_ai_worker,
)
from backend.config import Settings
from backend.database import Base
from backend.models.ai_task import AiTask
from backend.services.ai_worker import AiWorkerService


@pytest.fixture
def ai_lifecycle_db():
    """Isolated in-memory database with a shared connection."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    yield {"engine": engine, "Session": TestSession}

    engine.dispose()


@pytest.fixture
def ai_services(tmp_path):
    """Real shared model services with the empty default catalogue."""
    return build_ai_model_services(
        Settings(_env_file=None, models_dir=str(tmp_path / "models"))
    )


def _settings(**overrides):
    values = {
        "ai_worker_poll_interval_seconds": 0.02,
        "ai_task_recovery_interval_seconds": 60.0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _app():
    return SimpleNamespace(state=SimpleNamespace())


def _seed_pending(Session, task_type="text_classification") -> str:
    task = AiTask(task_type=task_type, status="PENDING")
    db = Session()
    try:
        db.add(task)
        db.commit()
        return task.id
    finally:
        db.close()


def _seed_expired_processing(Session, task_type="text_classification") -> str:
    from datetime import datetime, timedelta, timezone

    task = AiTask(
        task_type=task_type,
        status="PROCESSING",
        claim_token="stale-token",
        worker_id="dead-worker",
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


def _get_task(Session, task_id) -> AiTask:
    db = Session()
    try:
        task = db.get(AiTask, task_id)
        assert task is not None
        return task
    finally:
        db.close()


class _TrackingFactory:
    """Session factory that records opens and closes."""

    def __init__(self, factory):
        self._factory = factory
        self.created = 0
        self.closed = 0

    def __call__(self):
        session = self._factory()
        self.created += 1
        original_close = session.close

        def close():
            self.closed += 1
            original_close()

        session.close = close
        return session


# ---- Runtime construction and validation ----


def test_runtime_rejects_invalid_intervals(ai_lifecycle_db):
    worker = AiWorkerService(lambda: ai_lifecycle_db["Session"]())

    for kwargs in (
        {"poll_interval": 0},
        {"poll_interval": -1},
        {"poll_interval": float("inf")},
        {"poll_interval": True},
        {"poll_interval": "5"},
        {"recovery_interval": 0},
        {"recovery_interval": float("nan")},
        {"recovery_batch_size": 0},
        {"recovery_batch_size": True},
        {"shutdown_timeout": -0.5},
    ):
        with pytest.raises(ValueError):
            AiWorkerRuntime(worker, **kwargs)


def test_runtime_rejects_invalid_worker_identity(ai_lifecycle_db):
    worker = AiWorkerService(lambda: ai_lifecycle_db["Session"]())

    with pytest.raises(TypeError):
        AiWorkerRuntime(object())
    with pytest.raises(ValueError):
        AiWorkerRuntime(worker, worker_id="   ")
    with pytest.raises(ValueError):
        AiWorkerRuntime(worker, worker_id="x" * 65)


def test_runtime_cannot_start_outside_event_loop(ai_lifecycle_db):
    worker = AiWorkerService(lambda: ai_lifecycle_db["Session"]())
    runtime = AiWorkerRuntime(worker)

    with pytest.raises(RuntimeError):
        runtime.start()

    assert runtime.is_running is False
    assert runtime.accepting_tasks is False


def test_settings_defaults_are_positive():
    config = Settings(_env_file=None)

    assert config.ai_worker_poll_interval_seconds > 0
    assert config.ai_task_recovery_interval_seconds > 0


# ---- Synchronous cycle behavior (throttling, failure isolation) ----


def test_recovery_runs_only_when_due(ai_lifecycle_db, monkeypatch):
    worker = AiWorkerService(lambda: ai_lifecycle_db["Session"]())
    calls = []

    def counting_recover(*, batch_size=10):
        calls.append(batch_size)
        return 0

    monkeypatch.setattr(worker, "recover_expired_tasks", counting_recover)
    clock = {"now": 1000.0}
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: clock["now"])

    runtime = AiWorkerRuntime(worker, recovery_interval=60.0)
    # A cycle only runs while the runtime is accepting work; start() needs a
    # loop, so enable acceptance directly for this synchronous test.
    runtime._accepting = True

    assert runtime.run_cycle() == 0
    assert runtime.run_cycle() == 0
    assert len(calls) == 1

    clock["now"] += 60.0
    assert runtime.run_cycle() == 0
    assert len(calls) == 2


def test_recovery_failure_does_not_block_processing(ai_lifecycle_db, monkeypatch):
    Session = ai_lifecycle_db["Session"]
    worker = AiWorkerService(lambda: Session())
    worker.register_processor("text_classification", lambda task_id: {"tags": ["a"]})
    monkeypatch.setattr(
        worker,
        "recover_expired_tasks",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("recovery down")),
    )

    task_id = _seed_pending(Session)
    runtime = AiWorkerRuntime(worker)
    runtime._accepting = True

    assert runtime.run_cycle() == 1
    assert _get_task(Session, task_id).status == "AWAITING_APPROVAL"


def test_processor_failure_is_contained(ai_lifecycle_db):
    Session = ai_lifecycle_db["Session"]
    worker = AiWorkerService(lambda: Session())

    def failing_processor(task_id):
        raise RuntimeError("model exploded")

    worker.register_processor("text_classification", failing_processor)
    task_id = _seed_pending(Session)
    runtime = AiWorkerRuntime(worker)
    runtime._accepting = True

    assert runtime.run_cycle() == 1
    task = _get_task(Session, task_id)
    assert task.status == "FAILED"
    assert "model exploded" in task.error_message
    # The cycle itself did not raise and the next cycle is unaffected.
    assert runtime.run_cycle() == 0


def test_worker_call_opens_and_closes_its_session(ai_lifecycle_db):
    Session = ai_lifecycle_db["Session"]
    tracking = _TrackingFactory(Session)
    worker = AiWorkerService(tracking)
    worker.register_processor("text_classification", lambda task_id: {"ok": True})

    _seed_pending(Session)
    runtime = AiWorkerRuntime(worker)
    runtime._accepting = True

    assert runtime.run_cycle() == 1
    assert tracking.created >= 1
    assert tracking.created == tracking.closed


def test_stopped_runtime_does_not_claim_tasks(ai_lifecycle_db):
    Session = ai_lifecycle_db["Session"]
    worker = AiWorkerService(lambda: Session())
    worker.register_processor("text_classification", lambda task_id: {"ok": True})

    task_id = _seed_pending(Session)
    runtime = AiWorkerRuntime(worker)

    assert runtime.accepting_tasks is False
    assert runtime.run_cycle() == 0
    assert _get_task(Session, task_id).status == "PENDING"


# ---- Background lifecycle ----


async def test_background_loop_processes_a_task(ai_lifecycle_db):
    Session = ai_lifecycle_db["Session"]
    worker = AiWorkerService(lambda: Session())
    entered = threading.Event()
    proposals = []

    def processor(task_id):
        proposals.append(task_id)
        entered.set()
        return {"tags": ["processed"]}

    worker.register_processor("text_classification", processor)
    task_id = _seed_pending(Session)
    runtime = AiWorkerRuntime(worker, poll_interval=0.02, shutdown_timeout=10)

    assert runtime.start() is True
    assert runtime.accepting_tasks is True
    assert await asyncio.get_running_loop().run_in_executor(
        None, entered.wait, 5.0
    ), "processor was not invoked by the background loop"

    assert await runtime.stop() is True

    task = _get_task(Session, task_id)
    assert task.status == "AWAITING_APPROVAL"
    assert task.proposal_json == {"tags": ["processed"]}
    assert proposals == [task_id]
    assert runtime.is_running is False
    assert runtime.accepting_tasks is False


async def test_background_loop_recovers_expired_leases(ai_lifecycle_db, monkeypatch):
    Session = ai_lifecycle_db["Session"]
    worker = AiWorkerService(lambda: Session())
    recovered = threading.Event()
    original_recover = worker.recover_expired_tasks

    def recovering(**kwargs):
        count = original_recover(**kwargs)
        recovered.set()
        return count

    monkeypatch.setattr(worker, "recover_expired_tasks", recovering)
    task_id = _seed_expired_processing(Session)
    runtime = AiWorkerRuntime(worker, poll_interval=0.02, shutdown_timeout=10)

    runtime.start()
    assert await asyncio.get_running_loop().run_in_executor(
        None, recovered.wait, 5.0
    ), "recovery was not invoked by the background loop"
    await runtime.stop()

    task = _get_task(Session, task_id)
    assert task.status == "PENDING"
    assert task.retries == 2
    assert task.claim_token is None
    assert task.worker_id is None


async def test_shutdown_waits_for_in_flight_task(ai_lifecycle_db):
    Session = ai_lifecycle_db["Session"]
    worker = AiWorkerService(lambda: Session())
    started = threading.Event()
    release = threading.Event()

    def blocking_processor(task_id):
        started.set()
        assert release.wait(5.0)
        return {"finished": True}

    worker.register_processor("text_classification", blocking_processor)
    task_id = _seed_pending(Session)
    runtime = AiWorkerRuntime(worker, poll_interval=0.02, shutdown_timeout=10)
    runtime.start()

    loop = asyncio.get_running_loop()
    assert await loop.run_in_executor(None, started.wait, 5.0)

    stopping = asyncio.create_task(runtime.stop())
    await asyncio.sleep(0)
    # Acceptance stops immediately, but the in-flight task is awaited.
    assert runtime.accepting_tasks is False
    assert stopping.done() is False

    release.set()
    assert await asyncio.wait_for(stopping, timeout=5.0) is True

    task = _get_task(Session, task_id)
    assert task.status == "AWAITING_APPROVAL"
    assert task.proposal_json == {"finished": True}


async def test_second_start_does_not_create_a_duplicate_loop(ai_lifecycle_db):
    Session = ai_lifecycle_db["Session"]
    worker = AiWorkerService(lambda: Session())
    runtime = AiWorkerRuntime(worker, poll_interval=0.02)

    assert runtime.start() is True
    first_task = runtime._task
    assert runtime.start() is False
    assert runtime._task is first_task

    loops = [
        task
        for task in asyncio.all_tasks()
        if task.get_name() == "ai-worker-silo-ai-worker"
    ]
    assert len(loops) == 1

    assert await runtime.stop() is True
    # Idempotent shutdown.
    assert await runtime.stop() is False
    assert runtime.is_running is False


# ---- Application wiring ----


async def test_start_creates_one_shared_worker_and_stops(ai_lifecycle_db, ai_services):
    Session = ai_lifecycle_db["Session"]
    app = _app()

    runtime = start_ai_worker(
        app, _settings(), services=ai_services, session_factory=Session
    )

    assert runtime is not None
    assert app.state.ai_worker_runtime is runtime
    assert app.state.ai_worker is runtime.worker
    assert app.state.ai_worker_error is None
    assert isinstance(runtime.worker, AiWorkerService)
    assert runtime.worker_id == lifecycle.DEFAULT_AI_WORKER_ID
    # The empty default catalogue exposes no executable capability.
    assert runtime.task_types == []

    assert start_ai_worker(
        app, _settings(), services=ai_services, session_factory=Session
    ) is runtime

    assert await stop_ai_worker(app) is True
    assert app.state.ai_worker is None
    assert app.state.ai_worker_runtime is None
    assert await stop_ai_worker(app) is False


async def test_start_registers_available_processors(
    ai_lifecycle_db, ai_services, monkeypatch
):
    Session = ai_lifecycle_db["Session"]

    def fake_build(factory, services):
        worker = AiWorkerService(factory)
        worker.register_processor("text_classification", lambda task_id: {})
        return worker

    monkeypatch.setattr(lifecycle, "build_ai_worker", fake_build)
    app = _app()

    runtime = start_ai_worker(
        app, _settings(), services=ai_services, session_factory=Session
    )

    assert runtime is not None
    assert runtime.task_types == ["text_classification"]
    await stop_ai_worker(app)


async def test_missing_model_services_do_not_block_startup():
    app = _app()
    app.state.ai_models = None
    app.state.ai_models_error = "Model storage is unavailable"

    assert start_ai_worker(app, _settings()) is None
    assert app.state.ai_worker is None
    assert app.state.ai_worker_runtime is None
    assert "Model storage is unavailable" in app.state.ai_worker_error


async def test_construction_failure_is_recorded_and_cleaned_up(
    ai_services, monkeypatch
):
    def exploding_build(factory, services):
        raise RuntimeError("cannot compose processors")

    monkeypatch.setattr(lifecycle, "build_ai_worker", exploding_build)
    app = _app()
    app.state.ai_models = ai_services

    assert start_ai_worker(app, _settings()) is None
    assert app.state.ai_worker is None
    assert app.state.ai_worker_runtime is None
    assert app.state.ai_worker_error == "cannot compose processors"


async def test_invalid_interval_does_not_block_startup(ai_lifecycle_db, ai_services):
    app = _app()

    assert start_ai_worker(
        app,
        _settings(ai_worker_poll_interval_seconds=0.0),
        services=ai_services,
        session_factory=ai_lifecycle_db["Session"],
    ) is None
    assert app.state.ai_worker is None
    assert app.state.ai_worker_runtime is None
    assert "positive number of seconds" in app.state.ai_worker_error


def test_main_lifespan_wires_worker_start_and_stop_once():
    source = pathlib.Path("backend/main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]

    assert calls.count("start_ai_worker") == 1
    assert calls.count("stop_ai_worker") == 1
    assert "check_database_migrated" in source
