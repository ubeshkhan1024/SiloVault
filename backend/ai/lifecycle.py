"""Application lifespan integration for the local AI task worker.

This module owns the *runtime* around :class:`~backend.services.ai_worker.AiWorkerService`:

* it builds the worker from the one shared :class:`~backend.ai.bootstrap.AiModelServices`
  container with :func:`backend.ai.registration.build_ai_worker`,
* it runs exactly one background loop task that both recovers expired leases
  and processes tasks, so there are never two competing workers or loops,
* it stops cooperatively so shutdown does not abandon an in-flight task.

The worker and its processors are synchronous and use blocking SQLAlchemy
sessions, so each cycle runs in the default thread executor. Every worker call
opens and closes its own session; the runtime never holds one open.

The runtime is optional infrastructure: construction and startup failures are
recorded on ``app.state`` and never prevented from blocking the application.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import numbers
import time
from typing import Optional

from backend.ai.bootstrap import AiModelServices
from backend.ai.registration import build_ai_worker
from backend.config import settings as default_settings
from backend.database import SessionLocal
from backend.services.ai_worker import (
    DEFAULT_RECOVERY_BATCH_SIZE,
    AiWorkerService,
)

logger = logging.getLogger(__name__)

#: Worker identifier stored on claimed tasks (max 64 characters).
DEFAULT_AI_WORKER_ID = "silo-ai-worker"
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_RECOVERY_INTERVAL_SECONDS = 60.0
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 30.0


def _positive_seconds(value: object, name: str) -> float:
    """Validate a positive, finite duration expressed in seconds."""
    if (
        isinstance(value, bool)
        or not isinstance(value, numbers.Real)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a positive number of seconds")
    return float(value)


def _positive_int(value: object, name: str) -> int:
    """Validate a positive integer (``bool`` is not accepted)."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


class AiWorkerRuntime:
    """The single background loop for one configured AI worker.

    Each :meth:`run_cycle` first runs lease recovery when its interval has
    elapsed and then processes at most one task per registered task type. The
    loop never runs processing and recovery concurrently, and a second runtime
    is never started for a worker that is already running.
    """

    def __init__(
        self,
        worker: AiWorkerService,
        *,
        worker_id: str = DEFAULT_AI_WORKER_ID,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        recovery_interval: float = DEFAULT_RECOVERY_INTERVAL_SECONDS,
        recovery_batch_size: int = DEFAULT_RECOVERY_BATCH_SIZE,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(worker, AiWorkerService):
            raise TypeError("worker must be an AiWorkerService instance")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        worker_id = worker_id.strip()
        if len(worker_id) > 64:
            raise ValueError("worker_id must not exceed 64 characters")

        self._worker = worker
        self._worker_id = worker_id
        self._poll_interval = _positive_seconds(poll_interval, "poll_interval")
        self._recovery_interval = _positive_seconds(
            recovery_interval, "recovery_interval"
        )
        self._recovery_batch_size = _positive_int(
            recovery_batch_size, "recovery_batch_size"
        )
        self._shutdown_timeout = _positive_seconds(
            shutdown_timeout, "shutdown_timeout"
        )

        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._accepting = False
        self._last_recovery: Optional[float] = None

    @property
    def worker(self) -> AiWorkerService:
        return self._worker

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def task_types(self) -> list[str]:
        """Task types with a registered processor (empty when unavailable)."""
        return self._worker.get_registered_task_types()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def accepting_tasks(self) -> bool:
        """``False`` once shutdown started; no further task is claimed."""
        return self._accepting

    def start(self) -> bool:
        """Start the background loop.

        Returns:
            ``True`` when this call started the loop, ``False`` when it was
            already running (no duplicate loop or worker is created).

        Raises:
            RuntimeError: If called outside a running asyncio event loop.
        """
        if self._task is not None and not self._task.done():
            return False

        loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._accepting = True
        # Recover crash survivors immediately on the first cycle.
        self._last_recovery = None
        try:
            self._task = loop.create_task(
                self._run(), name=f"ai-worker-{self._worker_id}"
            )
        except Exception:
            self._accepting = False
            self._stop_event = None
            raise
        return True

    async def stop(self) -> bool:
        """Stop accepting tasks, let the current cycle finish, and join.

        Idempotent: a runtime that was never started, or that is already
        stopped, returns ``False`` without side effects.

        Returns:
            ``True`` when an active loop was stopped.
        """
        # Stop acceptance first: the loop will not claim another task.
        self._accepting = False
        if self._stop_event is not None:
            self._stop_event.set()

        task = self._task
        if task is None:
            return False
        self._task = None

        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=self._shutdown_timeout
            )
        except asyncio.TimeoutError:
            # A processor can outlive the bounded shutdown grace period. The
            # worker's own finally blocks still close its session; cancelling
            # only stops the loop from scheduling further cycles.
            logger.warning(
                "AI worker did not stop within %.1fs; cancelling its loop",
                self._shutdown_timeout,
            )
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise
        except Exception:
            logger.exception("AI worker loop ended with an error during shutdown")
        return True

    def run_cycle(self) -> int:
        """Run one synchronous recovery/processing cycle.

        Must be called from a worker thread (the background loop does), never
        from the event loop thread, because the worker and processors block.

        Returns:
            The number of tasks the worker finished in this cycle.
        """
        if not self._accepting:
            return 0

        self._recover_if_due()

        processed = 0
        for task_type in self._worker.get_registered_task_types():
            if not self._accepting:
                break
            try:
                if self._worker.process_one_task(self._worker_id, task_type):
                    processed += 1
            except Exception:
                # One failing task type must not stop the other task types or
                # the loop; the task itself is left to lease recovery.
                logger.exception(
                    "AI worker failed while processing a '%s' task", task_type
                )
        return processed

    def _recover_if_due(self) -> None:
        now = time.monotonic()
        if (
            self._last_recovery is not None
            and now - self._last_recovery < self._recovery_interval
        ):
            return
        self._last_recovery = now
        try:
            recovered = self._worker.recover_expired_tasks(
                batch_size=self._recovery_batch_size
            )
        except Exception:
            # Recovery is best-effort; processing and the loop continue.
            logger.exception("AI task lease recovery failed")
            return
        if recovered:
            logger.info("Recovered %d expired AI task lease(s)", recovered)

    async def _run(self) -> None:
        stop_event = self._stop_event
        if stop_event is None:
            return
        loop = asyncio.get_running_loop()
        try:
            while not stop_event.is_set():
                try:
                    await loop.run_in_executor(None, self.run_cycle)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Defensive: run_cycle already isolates worker failures.
                    logger.exception("AI worker cycle failed")

                if stop_event.is_set():
                    break
                # Awaiting the stop event means shutdown wakes the loop
                # immediately instead of waiting out the poll interval.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=self._poll_interval
                    )
        finally:
            self._accepting = False


def start_ai_worker(
    app,
    settings=None,
    *,
    services: Optional[AiModelServices] = None,
    session_factory=None,
) -> Optional[AiWorkerRuntime]:
    """Build and start the application's single AI worker runtime.

    The shared model services already attached by
    :func:`backend.ai.bootstrap.configure_ai_model_services` are reused; no
    second service container is created. Missing services, an invalid
    configuration, or a construction error are recorded on
    ``app.state.ai_worker_error`` and never block application startup.

    Returns:
        The running runtime, or ``None`` when the worker could not start.
    """
    config = default_settings if settings is None else settings

    existing = getattr(app.state, "ai_worker_runtime", None)
    if isinstance(existing, AiWorkerRuntime) and existing.is_running:
        return existing

    if services is None:
        services = getattr(app.state, "ai_models", None)
    if services is None:
        reason = (
            getattr(app.state, "ai_models_error", None)
            or "AI model services are unavailable"
        )
        app.state.ai_worker = None
        app.state.ai_worker_runtime = None
        app.state.ai_worker_error = f"AI worker not started: {reason}"
        logger.warning("AI worker not started: %s", reason)
        return None

    factory = SessionLocal if session_factory is None else session_factory
    try:
        worker = build_ai_worker(factory, services)
        runtime = AiWorkerRuntime(
            worker,
            poll_interval=config.ai_worker_poll_interval_seconds,
            recovery_interval=config.ai_task_recovery_interval_seconds,
        )
        runtime.start()
    except Exception as exc:
        # Partial initialization is discarded; the app remains usable.
        app.state.ai_worker = None
        app.state.ai_worker_runtime = None
        app.state.ai_worker_error = str(exc)
        logger.warning("AI worker not started: %s", exc)
        return None

    app.state.ai_worker = worker
    app.state.ai_worker_runtime = runtime
    app.state.ai_worker_error = None
    logger.info(
        "AI worker '%s' started with %d processor(s)",
        runtime.worker_id,
        len(runtime.task_types),
    )
    return runtime


async def stop_ai_worker(app) -> bool:
    """Stop the application's AI worker, if one is attached.

    Idempotent: calling it again (or on an app that never started a worker)
    returns ``False`` and does nothing.
    """
    runtime = getattr(app.state, "ai_worker_runtime", None)
    app.state.ai_worker_runtime = None
    app.state.ai_worker = None
    if runtime is None:
        return False
    await runtime.stop()
    return True


__all__ = [
    "AiWorkerRuntime",
    "DEFAULT_AI_WORKER_ID",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_RECOVERY_INTERVAL_SECONDS",
    "DEFAULT_SHUTDOWN_TIMEOUT_SECONDS",
    "start_ai_worker",
    "stop_ai_worker",
]
