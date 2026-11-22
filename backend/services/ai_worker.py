"""AI task worker service for atomic task claiming."""

from __future__ import annotations

import math
import numbers
import threading
import uuid
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from sqlalchemy import update, select, func, case, null, not_, and_, or_
from sqlalchemy.orm import Session

from backend.models.ai_task import AiTask
from backend.database import Base

# Lease renewal defaults. A claimed task holds a 300-second lease; the
# heartbeat renews it well before expiry so a long-running processor keeps
# ownership of its task.
DEFAULT_LEASE_HEARTBEAT_INTERVAL_SECONDS = 100.0
_LEASE_HEARTBEAT_JOIN_TIMEOUT_SECONDS = 5.0

# Expired-lease recovery defaults.
DEFAULT_RECOVERY_BATCH_SIZE = 10
RECOVERY_LEASE_EXPIRED_MESSAGE = (
    "Lease expired before the task completed; retries exhausted."
)


def _expired_lease_condition(now: datetime):
    """SQL condition for a lease that can no longer belong to an active worker.

    A missing lease on a PROCESSING row means ownership cannot be
    established, so it is treated as expired (the same convention as
    ``PendingDeletion`` recovery).
    """
    return or_(
        AiTask.lease_expires_at.is_(None),
        AiTask.lease_expires_at <= now,
    )


class _LeaseHeartbeat:
    """Background lease renewal for a single claimed AI task.

    Each renewal calls ``renew``, which is expected to open, commit and close
    its own database session. The heartbeat thread therefore never touches the
    processor's or the main worker's SQLAlchemy session.

    Ownership is considered lost if a renewal returns ``False`` (the row no
    longer matches this worker/claim token or the lease expired) or raises. In
    both cases the loop stops so the worker fails closed and does not finalize
    the task.
    """

    def __init__(
        self,
        renew: Callable[[str, str, str], bool],
        task_id: str,
        worker_id: str,
        claim_token: str,
        interval: float,
    ) -> None:
        self._renew = renew
        self._task_id = task_id
        self._worker_id = worker_id
        self._claim_token = claim_token
        self._interval = interval
        self._stop_event = threading.Event()
        self._ownership_lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"ai-lease-heartbeat-{task_id}",
            daemon=True,
        )

    @property
    def ownership_lost(self) -> bool:
        return self._ownership_lost.is_set()

    def start(self) -> None:
        """Start renewing the lease in the background."""
        self._thread.start()

    def stop(self) -> None:
        """Stop renewal and join the heartbeat thread."""
        self._stop_event.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=_LEASE_HEARTBEAT_JOIN_TIMEOUT_SECONDS)
        if self._thread.is_alive():
            # Fail closed: a renewal may still be in flight while the worker
            # is about to finalize the task.
            self._ownership_lost.set()

    def _run(self) -> None:
        # Wait first: a task that finishes quickly never renews or contends
        # with the main worker's session.
        while not self._stop_event.wait(self._interval):
            try:
                renewed = self._renew(
                    self._task_id, self._worker_id, self._claim_token
                )
            except Exception:
                # A broker/database failure must not leave this worker
                # believing it still owns the task.
                self._ownership_lost.set()
                return

            if not renewed:
                self._ownership_lost.set()
                return


class AiWorkerService:
    """Service for claiming and processing AI tasks atomically."""

    def __init__(
        self,
        db_factory,
        *,
        lease_heartbeat_interval: float = DEFAULT_LEASE_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        if (
            isinstance(lease_heartbeat_interval, bool)
            or not isinstance(lease_heartbeat_interval, numbers.Real)
            or not math.isfinite(lease_heartbeat_interval)
            or lease_heartbeat_interval <= 0
        ):
            raise ValueError(
                "lease_heartbeat_interval must be a positive number of seconds"
            )
        self._db_factory = db_factory
        self._lease_heartbeat_interval = float(lease_heartbeat_interval)
        # Processor registry: task_type -> callable
        # Processors accept (task_id: str) and return a proposal dictionary.
        self._processors: dict[str, Callable[[str], dict[str, Any]]] = {}
        
    def claim_one_pending_task(self, worker_id: str) -> Optional[AiTask]:
        """Claim the oldest eligible PENDING AiTask atomically.

        Uses a conditional UPDATE with .returning() so two workers cannot
        claim the same task. The WHERE clause rechecks eligibility at update
        time, preventing race conditions.

        The subquery selects the oldest task (by created_at, then id as
        tie-breaker). The outer UPDATE rechecks that the task is still PENDING,
        ensuring the claim is truly atomic.

        Args:
            worker_id: Identifier for the worker claiming the task.
                Must be a non-empty string not exceeding 64 characters.

        Returns:
            The claimed AiTask instance if successful, None if no eligible
            tasks exist or another worker claimed the task first.

        Raises:
            ValueError: If worker_id is empty, whitespace-only, too long,
                or not a string.
        """
        # --- Worker ID validation ---
        if not isinstance(worker_id, str):
            raise ValueError("worker_id must be a non-empty string")
        worker_id = worker_id.strip()
        if not worker_id:
            raise ValueError("worker_id must be a non-empty string")
        if len(worker_id) > 64:
            raise ValueError("worker_id must not exceed 64 characters")

        now = datetime.now(timezone.utc)
        claim_token = str(uuid.uuid4())
        lease_expires_at = now + timedelta(seconds=300)  # 5 minutes

        db: Session = self._db_factory()
        try:
            # Atomically claim the oldest eligible PENDING task.
            # The subquery selects the oldest task (by created_at, then id as tie-breaker).
            # The outer WHERE rechecks that the task is still PENDING, ensuring
            # the UPDATE is truly conditional and atomic.
            select_subquery = (
                select(AiTask.id)
                .where(
                    AiTask.status == "PENDING",
                    AiTask.claim_token.is_(None),  # Not already claimed
                    AiTask.worker_id.is_(None),    # Not already assigned
                )
                .order_by(AiTask.created_at.asc(), AiTask.id.asc())  # Oldest first, deterministic tie-break
                .limit(1)
                .subquery()
            )

            update_stmt = (
                update(AiTask)
                .where(
                    AiTask.id.in_(select(select_subquery.c.id)),
                    AiTask.status == "PENDING",  # Recheck: still PENDING?
                )
                .values(
                    status="PROCESSING",
                    claim_token=claim_token,
                    worker_id=worker_id,
                    started_at=now,
                    lease_expires_at=lease_expires_at,
                )
                .returning(AiTask)
            )

            result = db.execute(update_stmt)
            db.commit()

            claimed_task = result.scalar_one_or_none()
            return claimed_task

        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ---- Processor Registry ----

    def register_processor(self,task_type: str,processor: Callable[[str], dict[str, Any]],) -> None:
        """Register a processor callable for a given task type.

        Registering the identical callable again is a no-op. Registering a
        *different* callable for an already-registered task type raises
        :class:`ValueError`, so two composition paths cannot silently
        overwrite each other's processor.

        Args:
            task_type: The task type string (e.g. "text_classification").
            processor: A callable that accepts a task_id string and returns a dictionary containing proposed metadata.

        Raises:
            ValueError: If task_type is empty/whitespace, if processor is
                not callable, or if a different processor is already
                registered for task_type.
        """
        if not isinstance(task_type, str) or not task_type.strip():
            raise ValueError("task_type must be a non-empty string")
        task_type = task_type.strip()
        if not callable(processor):
            raise ValueError("processor must be a callable")

        existing = self._processors.get(task_type)
        if existing is not None:
            if existing is processor:
                return
            raise ValueError(
                "A different processor is already registered for "
                f"task_type: {task_type}"
            )
        self._processors[task_type] = processor

    def get_processor(
        self, task_type: str
    ) -> Optional[Callable[[str], dict[str, Any]]]:
        """Resolve the processor registered for *task_type*, if any.

        Args:
            task_type: The task type string to resolve.

        Returns:
            The registered callable, or ``None`` when the task type is not
            supported by this worker.

        Raises:
            ValueError: If task_type is empty, whitespace-only, or not a
                string.
        """
        if not isinstance(task_type, str) or not task_type.strip():
            raise ValueError("task_type must be a non-empty string")
        return self._processors.get(task_type.strip())

    def unregister_processor(self, task_type: str) -> None:
        """Unregister a processor for a given task type.

        Args:
            task_type: The task type string to unregister.

        Raises:
            KeyError: If no processor is registered for the given task_type.
        """
        if task_type not in self._processors:
            raise KeyError(f"No processor registered for task_type: {task_type}")
        del self._processors[task_type]

    def get_registered_task_types(self) -> list[str]:
        """Return a copy of the currently registered task type strings.

        Returns:
            An immutable copy of the set of registered task type names.
        """
        return list(self._processors.keys())

    # ---- Lease Renewal ----

    def renew_task_lease(self, task_id: str, worker_id: str, claim_token: str) -> bool:
        """Renew the lease for an AI task.

        Uses a conditional atomic UPDATE so only the task that matches all
        eligibility criteria is renewed. Two concurrent callers with different
        credentials cannot both succeed.

        The task is renewed only when ALL of the following match:
            - task ID matches
            - status is PROCESSING
            - worker_id matches
            - claim_token matches
            - lease_expires_at is strictly later than current UTC time

        On success the lease is extended by 300 seconds (5 minutes).

        Args:
            task_id: The UUID string of the AiTask to renew.
            worker_id: The worker identifier currently associated with the task.
            claim_token: The claim token currently associated with the task.

        Returns:
            True if exactly one row was renewed; False if the task was not
            eligible for renewal (wrong status, wrong worker, wrong token,
            expired lease, or task not found).

        Raises:
            ValueError: If any input is invalid (wrong type, empty, etc.).
        """
        # --- Input validation ---
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id must be a non-empty string")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        worker_id = worker_id.strip()
        if not isinstance(claim_token, str) or not claim_token:
            raise ValueError("claim_token must be a non-empty string")

        now = datetime.now(timezone.utc)
        new_lease_expires_at = now + timedelta(seconds=300)  # 5 minutes

        db: Session = self._db_factory()
        try:
            # Atomic conditional UPDATE: only renew when all criteria match.
            # - status is PROCESSING
            # - worker_id matches the current worker
            # - claim_token matches the current token
            # - lease_expires_at is strictly in the future (not already expired)
            update_stmt = (
                update(AiTask)
                .where(
                    AiTask.id == task_id,
                    AiTask.status == "PROCESSING",
                    AiTask.worker_id == worker_id,
                    AiTask.claim_token == claim_token,
                    AiTask.lease_expires_at > now,  # lease must still be valid
                )
                .values(
                    lease_expires_at=new_lease_expires_at,
                )
                .returning(AiTask.id)
            )

            result = db.execute(update_stmt)
            updated_id = result.scalar_one_or_none()  # noqa: E712  # noqa: E712
            db.commit()

            # Return True only when exactly one row was renewed.
            # scalar_one_or_none() returns the AiTask.id when one row matches,
            # None when zero rows match. Since we use .where(AiTask.id == task_id)
            # (PK filter), at most one row can match.
            return updated_id is not None  # noqa: E712

        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ---- Expired-Lease Recovery ----

    def recover_expired_tasks(
        self, batch_size: int = DEFAULT_RECOVERY_BATCH_SIZE
    ) -> int:
        """Recover tasks abandoned in ``PROCESSING`` after their lease expired.

        A task is recoverable only when its status is ``PROCESSING`` and its
        lease is missing or no later than the current UTC time. Tasks whose
        lease is still valid are left untouched, so an active worker is never
        reclaimed.

        Recovery is a single conditional UPDATE, so it is atomic and
        idempotent: concurrent recovery attempts, renewals or cancellations
        make the rechecked WHERE clause fail for that row, and the first caller
        wins. The transition depends on the row state only, never on a
        previously read snapshot.

        Outcomes, in priority order:
            * cancellation was requested -> ``CANCELLED`` (never retried)
            * ``retries < max_retries``   -> ``PENDING`` with ``retries + 1``
            * retries exhausted           -> ``FAILED`` (terminal)

        In every case ownership fields are cleared, so a stale worker that
        still holds the old claim token can no longer renew or finalize the
        task. Proposals are never applied and no file data is touched.

        Args:
            batch_size: Maximum number of tasks transitioned in one call.

        Returns:
            The number of tasks transitioned out of ``PROCESSING``.

        Raises:
            ValueError: If ``batch_size`` is not a positive integer.
        """
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or batch_size <= 0
        ):
            raise ValueError("batch_size must be a positive integer")

        now = datetime.now(timezone.utc)
        db: Session = self._db_factory()
        try:
            candidate_ids = self._select_expired_task_ids(db, now, batch_size)
            if not candidate_ids:
                return 0

            recovered = self._transition_expired_tasks(db, candidate_ids, now)
            db.commit()
            return recovered

        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _select_expired_task_ids(
        db: Session, now: datetime, batch_size: int
    ) -> list[str]:
        """Read-only scan for expired PROCESSING tasks, oldest lease first."""
        stmt = (
            select(AiTask.id)
            .where(
                AiTask.status == "PROCESSING",
                _expired_lease_condition(now),
            )
            .order_by(AiTask.lease_expires_at.asc(), AiTask.id.asc())
            .limit(batch_size)
        )
        return [row[0] for row in db.execute(stmt).all()]

    @staticmethod
    def _transition_expired_tasks(
        db: Session, candidate_ids: list[str], now: datetime
    ) -> int:
        """Atomically transition the given candidates that are still expired.

        The WHERE clause rechecks status and lease expiry, so a task that was
        renewed, cancelled, finalized or already recovered after the scan is
        skipped instead of being overwritten.
        """
        cancelled = AiTask.cancellation_requested_at.is_not(None)
        retryable = and_(
            not_(cancelled), AiTask.retries < AiTask.max_retries
        )

        stmt = (
            update(AiTask)
            .where(
                AiTask.id.in_(candidate_ids),
                AiTask.status == "PROCESSING",
                _expired_lease_condition(now),
            )
            .values(
                status=case(
                    (cancelled, "CANCELLED"),
                    (retryable, "PENDING"),
                    else_="FAILED",
                ),
                retries=case(
                    (retryable, AiTask.retries + 1),
                    else_=AiTask.retries,
                ),
                error_message=case(
                    (cancelled, AiTask.error_message),
                    (retryable, AiTask.error_message),
                    else_=RECOVERY_LEASE_EXPIRED_MESSAGE,
                ),
                claim_token=null(),
                worker_id=null(),
                lease_expires_at=null(),
                started_at=case((retryable, null()), else_=AiTask.started_at),
                completed_at=case((retryable, null()), else_=now),
            )
        )
        return db.execute(stmt).rowcount

    # Export the service from the services package


    def process_one_task(self, worker_id: str, task_type: str) -> bool:
        """Claim and safely execute one task of a registered type."""

        if not isinstance(task_type, str) or not task_type.strip():
            raise ValueError("task_type must be a non-empty string")

        task_type = task_type.strip()
        # An unsupported task type is reported before any claim, so its row
        # is never left in PROCESSING with no processor able to finish it.
        processor = self.get_processor(task_type)

        if processor is None:
            return False

        task = self.claim_one_pending_task_by_type(worker_id, task_type)

        if task is None:
            return False

        task_id = task.id
        claim_token = task.claim_token

        proposal = None
        processing_error = None

        # Do not start work if cancellation was already requested.
        if task.cancellation_requested_at is None:
            heartbeat = _LeaseHeartbeat(
                renew=self.renew_task_lease,
                task_id=task_id,
                worker_id=worker_id.strip(),
                claim_token=claim_token,
                interval=self._lease_heartbeat_interval,
            )

            heartbeat.start()
            try:
                proposal = processor(task_id)

                if not isinstance(proposal, dict):
                    raise TypeError("Processor must return a dictionary")

                # Ensure the proposal can be stored as JSON.
                json.dumps(proposal)

            except Exception as exc:
                processing_error = str(exc)[:1024]
            finally:
                # Always stop and join before finalization so no renewal can
                # race the ownership-checked final update.
                heartbeat.stop()

            # If this worker no longer owns the task, leave the row untouched
            # for whoever owns it now (or for lease recovery).
            if heartbeat.ownership_lost:
                return False

        now = datetime.now(timezone.utc)
        db: Session = self._db_factory()

        try:
            # Common ownership and lease checks.
            conditions = (
                AiTask.id == task_id,
                AiTask.status == "PROCESSING",
                AiTask.worker_id == worker_id.strip(),
                AiTask.claim_token == claim_token,
                AiTask.lease_expires_at > now,
            )

            # Cancellation always takes priority.
            cancel_stmt = (
                update(AiTask)
                .where(
                    *conditions,
                    AiTask.cancellation_requested_at.is_not(None),
                )
                .values(
                    status="CANCELLED",
                    completed_at=now,
                    claim_token=None,
                    worker_id=None,
                    lease_expires_at=None,
                )
                .returning(AiTask.id)
            )

            cancelled = db.execute(cancel_stmt).scalar_one_or_none()

            if cancelled is not None:
                db.commit()
                return True

            # A failed processor must not overwrite a cancellation.
            if processing_error is not None:
                final_stmt = (
                    update(AiTask)
                    .where(
                        *conditions,
                        AiTask.cancellation_requested_at.is_(None),
                    )
                    .values(
                        status="FAILED",
                        error_message=processing_error,
                        completed_at=now,
                        claim_token=None,
                        worker_id=None,
                        lease_expires_at=None,
                    )
                    .returning(AiTask.id)
                )
            else:
                final_stmt = (
                    update(AiTask)
                    .where(
                        *conditions,
                        AiTask.cancellation_requested_at.is_(None),
                    )
                    .values(
                        status="AWAITING_APPROVAL",
                        proposal_json=proposal,
                        completed_at=now,
                        claim_token=None,
                        worker_id=None,
                        lease_expires_at=None,
                    )
                    .returning(AiTask.id)
                )

            finalized = db.execute(final_stmt).scalar_one_or_none()

            if finalized is not None:
                db.commit()
                return True

            # Cancellation may have arrived between the first check
            # and the attempted final update.
            cancelled = db.execute(cancel_stmt).scalar_one_or_none()
            db.commit()

            return cancelled is not None

        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ---- Task Type-Aware Claiming ----

    def claim_one_pending_task_by_type(self, worker_id: str, task_type: str) -> Optional[AiTask]:
        """Claim the oldest PENDING task of a given type, if a processor is registered.

        Only tasks whose type has a registered processor can be claimed this way.
        If the task type is not registered, this method always returns None.

        Args:
            worker_id: Identifier for the worker claiming the task.
                Must be a non-empty string not exceeding 64 characters.
            task_type: The task type to filter by.

        Returns:
            The claimed AiTask instance if successful, None if no eligible
            tasks of the given type exist, or if the task type is not registered.

        Raises:
            ValueError: If worker_id is empty, whitespace-only, too long,
                or not a string.
        """
        # Validate worker_id
        if not isinstance(worker_id, str):
            raise ValueError("worker_id must be a non-empty string")
        worker_id = worker_id.strip()
        if not worker_id:
            raise ValueError("worker_id must be a non-empty string")
        if len(worker_id) > 64:
            raise ValueError("worker_id must not exceed 64 characters")

        # Validate task_type
        if not isinstance(task_type, str):
            raise ValueError("task_type must be a non-empty string")

        task_type = task_type.strip()

        if not task_type:
            raise ValueError("task_type must be a non-empty string")

        # Check if a processor is registered for this task type
        if task_type not in self._processors:
            return None

        now = datetime.now(timezone.utc)
        claim_token = str(uuid.uuid4())
        lease_expires_at = now + timedelta(seconds=300)  # 5 minutes

        db: Session = self._db_factory()
        try:
            # Claim the oldest PENDING task of the given type
            select_subquery = (
                select(AiTask.id)
                .where(
                    AiTask.status == "PENDING",
                    AiTask.claim_token.is_(None),  # Not already claimed
                    AiTask.worker_id.is_(None),    # Not already assigned
                    AiTask.task_type == task_type,  # Filter by task type
                )
                .order_by(AiTask.created_at.asc(), AiTask.id.asc())  # Oldest first, deterministic tie-break
                .limit(1)
                .subquery()
            )

            update_stmt = (
                update(AiTask)
                .where(
                    AiTask.id.in_(select(select_subquery.c.id)),
                    AiTask.status == "PENDING",  # Recheck: still PENDING?
                )
                .values(
                    status="PROCESSING",
                    claim_token=claim_token,
                    worker_id=worker_id,
                    started_at=now,
                    lease_expires_at=lease_expires_at,
                )
                .returning(AiTask)
            )

            result = db.execute(update_stmt)
            db.commit()

            claimed_task = result.scalar_one_or_none()
            return claimed_task

        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


# Export the service from the services package
__all__ = ["AiWorkerService"]