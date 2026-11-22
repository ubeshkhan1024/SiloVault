"""Tests for expired-lease recovery of AI tasks (M4.4 Step 3B)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models.ai_task import AiTask
from backend.services.ai_worker import (
    DEFAULT_RECOVERY_BATCH_SIZE,
    RECOVERY_LEASE_EXPIRED_MESSAGE,
    AiWorkerService,
)


@pytest.fixture
def ai_recovery_db():
    """Isolated SQLite database, session factory and worker service."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    service = AiWorkerService(lambda: TestSession())

    yield {
        "engine": engine,
        "Session": TestSession,
        "service": service,
    }

    engine.dispose()


def _seed_task(
    Session,
    *,
    status: str = "PROCESSING",
    lease_seconds: float | None = -60,
    retries: int = 0,
    max_retries: int = 3,
    claim_token: str | None = "claim-token",
    worker_id: str | None = "worker-1",
    cancellation_requested_at: datetime | None = None,
    proposal_json: dict | None = None,
    error_message: str | None = None,
) -> str:
    """Seed one AI task with an explicit lease offset (negative = expired)."""
    task = AiTask(
        task_type="text_classification",
        status=status,
        retries=retries,
        max_retries=max_retries,
        claim_token=claim_token if status == "PROCESSING" else None,
        worker_id=worker_id if status == "PROCESSING" else None,
        lease_expires_at=(
            datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
            if lease_seconds is not None
            else None
        ),
        cancellation_requested_at=cancellation_requested_at,
        proposal_json=proposal_json,
        error_message=error_message,
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


# ---- Core recovery behavior ----


def test_recover_expired_processing_task(ai_recovery_db):
    """An expired PROCESSING task is requeued with its retry count incremented."""
    Session = ai_recovery_db["Session"]
    service = ai_recovery_db["service"]
    task_id = _seed_task(Session, lease_seconds=-30, retries=1)

    assert service.recover_expired_tasks() == 1

    task = _get_task(Session, task_id)
    assert task.status == "PENDING"
    assert task.retries == 2
    assert task.claim_token is None
    assert task.worker_id is None
    assert task.lease_expires_at is None
    assert task.started_at is None
    assert task.completed_at is None
    assert task.proposal_json is None


def test_recover_task_with_missing_lease(ai_recovery_db):
    """A PROCESSING row without a lease cannot be owned and is recovered."""
    Session = ai_recovery_db["Session"]
    service = ai_recovery_db["service"]
    task_id = _seed_task(Session, lease_seconds=None)

    assert service.recover_expired_tasks() == 1
    assert _get_task(Session, task_id).status == "PENDING"


def test_active_task_with_valid_lease_is_preserved(ai_recovery_db):
    """A task whose lease is still valid is never reclaimed."""
    Session = ai_recovery_db["Session"]
    service = ai_recovery_db["service"]
    original_lease = datetime.now(timezone.utc) + timedelta(seconds=300)
    task_id = _seed_task(Session, lease_seconds=300)

    assert service.recover_expired_tasks() == 0

    task = _get_task(Session, task_id)
    assert task.status == "PROCESSING"
    assert task.claim_token == "claim-token"
    assert task.worker_id == "worker-1"
    assert task.retries == 0
    assert task.lease_expires_at > original_lease - timedelta(seconds=5)


def test_mixed_batch_only_recovers_expired_tasks(ai_recovery_db):
    """Only the expired task in a mixed batch is transitioned."""
    Session = ai_recovery_db["Session"]
    service = ai_recovery_db["service"]
    expired_id = _seed_task(Session, lease_seconds=-5)
    active_id = _seed_task(Session, lease_seconds=300)

    assert service.recover_expired_tasks() == 1

    assert _get_task(Session, expired_id).status == "PENDING"
    assert _get_task(Session, active_id).status == "PROCESSING"


def test_recovery_respects_batch_size(ai_recovery_db):
    """At most batch_size tasks are transitioned per call."""
    Session = ai_recovery_db["Session"]
    service = ai_recovery_db["service"]
    for _ in range(3):
        _seed_task(Session, lease_seconds=-10)

    assert service.recover_expired_tasks(batch_size=2) == 2
    assert service.recover_expired_tasks(batch_size=2) == 1


def test_recovery_is_idempotent(ai_recovery_db):
    """A second recovery pass makes no further changes."""
    Session = ai_recovery_db["Session"]
    service = ai_recovery_db["service"]
    task_id = _seed_task(Session, lease_seconds=-10, retries=1)

    assert service.recover_expired_tasks() == 1
    first = _get_task(Session, task_id)
    assert first.retries == 2

    assert service.recover_expired_tasks() == 0

    second = _get_task(Session, task_id)
    assert second.status == "PENDING"
    assert second.retries == 2
    assert second.claim_token is None


# ---- Worker failure and ownership ----


def test_recovery_after_simulated_worker_failure(ai_recovery_db):
    """A claimed task abandoned by a crashed worker is requeued and reprocessed."""
    Session = ai_recovery_db["Session"]
    service = ai_recovery_db["service"]
    service.register_processor(
        "text_classification", lambda task_id: {"category": "document"}
    )
    task_id = _seed_task(Session, status="PENDING", lease_seconds=None)

    claimed = service.claim_one_pending_task_by_type(
        "worker-1", "text_classification"
    )
    assert claimed is not None and claimed.id == task_id
    abandoned_token = claimed.claim_token

    # Simulate a crash: the lease expires and nothing is ever finalized.
    db = Session()
    try:
        db.execute(
            update(AiTask)
            .where(AiTask.id == task_id)
            .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        db.commit()
    finally:
        db.close()

    assert service.recover_expired_tasks() == 1
    assert _get_task(Session, task_id).status == "PENDING"
    assert service.renew_task_lease(task_id, "worker-1", abandoned_token) is False

    # The recovered task is picked up and completed normally.
    assert service.process_one_task("worker-1", "text_classification") is True

    task = _get_task(Session, task_id)
    assert task.status == "AWAITING_APPROVAL"
    assert task.proposal_json == {"category": "document"}
    assert task.retries == 1
    assert task.claim_token is None


def test_stale_claim_token_cannot_renew_or_finalize_after_recovery(ai_recovery_db):
    """The crashed worker's token is dead once recovery has requeued the task."""
    Session = ai_recovery_db["Session"]
    service = ai_recovery_db["service"]

    service.register_processor(
        "text_classification", lambda task_id: {"category": "stale"}
    )
    task_id = _seed_task(Session, status="PENDING", lease_seconds=None)
    claimed = service.claim_one_pending_task_by_type(
        "worker-1", "text_classification"
    )
    assert claimed is not None
    stale_token = claimed.claim_token

    db = Session()
    try:
        db.execute(
            update(AiTask)
            .where(AiTask.id == task_id)
            .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        db.commit()
    finally:
        db.close()

    assert service.recover_expired_tasks() == 1

    # Stale ownership cannot renew ...
    assert service.renew_task_lease(task_id, "worker-1", stale_token) is False

    # ... and a new worker can claim the requeued task.
    reclaimed = service.claim_one_pending_task_by_type(
        "worker-2", "text_classification"
    )
    assert reclaimed is not None and reclaimed.id == task_id
    assert reclaimed.claim_token != stale_token

    # The old token still cannot renew or overwrite the newer worker's row.
    assert service.renew_task_lease(task_id, "worker-1", stale_token) is False
    task = _get_task(Session, task_id)
    assert task.status == "PROCESSING"
    assert task.worker_id == "worker-2"
    assert task.claim_token == reclaimed.claim_token


def test_concurrent_recovery_attempts_transition_once(ai_recovery_db):
    """A competing recovery pass between scan and update cannot double-apply.

    The second worker is injected during the first worker's read-only scan,
    which deterministically reproduces the race window without threads.
    """
    Session = ai_recovery_db["Session"]
    service = ai_recovery_db["service"]
    competing_service = AiWorkerService(lambda: Session())

    task_id = _seed_task(Session, lease_seconds=-10, retries=0)

    original_select = AiWorkerService._select_expired_task_ids
    competing_results = []

    def select_with_competing_worker(db, now, batch_size):
        candidate_ids = original_select(db, now, batch_size)
        # Another recovery worker wins the race for the same candidate.
        competing_results.append(competing_service.recover_expired_tasks())
        return candidate_ids

    service._select_expired_task_ids = select_with_competing_worker

    assert service.recover_expired_tasks() == 0
    assert competing_results == [1]

    task = _get_task(Session, task_id)
    assert task.status == "PENDING"
    assert task.retries == 1


def test_recovery_skips_task_renewed_before_update(ai_recovery_db):
    """A lease renewed between scan and update keeps its ownership."""
    Session = ai_recovery_db["Session"]
    service = ai_recovery_db["service"]
    task_id = _seed_task(Session, lease_seconds=-10)

    original_select = AiWorkerService._select_expired_task_ids

    def select_with_renewal(db, now, batch_size):
        candidate_ids = original_select(db, now, batch_size)
        renewed = service.renew_task_lease(task_id, "worker-1", "claim-token")
        # Renewal is rejected for an already-expired lease ...
        assert renewed is False
        # ... so simulate a renewal that committed just before the scan.
        renew_db = Session()
        try:
            renew_db.execute(
                update(AiTask)
                .where(AiTask.id == task_id)
                .values(
                    lease_expires_at=datetime.now(timezone.utc)
                    + timedelta(seconds=300)
                )
            )
            renew_db.commit()
        finally:
            renew_db.close()
        return candidate_ids

    service._select_expired_task_ids = select_with_renewal

    assert service.recover_expired_tasks() == 0

    task = _get_task(Session, task_id)
    assert task.status == "PROCESSING"
    assert task.claim_token == "claim-token"
    assert task.worker_id == "worker-1"
    assert task.retries == 0


# ---- Retry limits, cancellation and terminal states ----


def test_retry_limit_marks_task_failed(ai_recovery_db):
    """An expired task with no retries left becomes terminally FAILED."""
    Session = ai_recovery_db["Session"]
    service = ai_recovery_db["service"]
    task_id = _seed_task(Session, lease_seconds=-10, retries=3, max_retries=3)

    assert service.recover_expired_tasks() == 1

    task = _get_task(Session, task_id)
    assert task.status == "FAILED"
    assert task.retries == 3
    assert task.error_message == RECOVERY_LEASE_EXPIRED_MESSAGE
    assert task.completed_at is not None
    assert task.claim_token is None
    assert task.worker_id is None
    assert task.lease_expires_at is None


def test_last_retry_is_still_requeued(ai_recovery_db):
    """retries == max_retries - 1 still gets one more attempt."""
    Session = ai_recovery_db["Session"]
    service = ai_recovery_db["service"]
    task_id = _seed_task(Session, lease_seconds=-10, retries=2, max_retries=3)

    assert service.recover_expired_tasks() == 1

    task = _get_task(Session, task_id)
    assert task.status == "PENDING"
    assert task.retries == 3


def test_cancellation_requested_wins_over_retry(ai_recovery_db):
    """A cancelled-but-abandoned task is CANCELLED, never requeued."""
    Session = ai_recovery_db["Session"]
    service = ai_recovery_db["service"]
    cancel_time = datetime.now(timezone.utc) - timedelta(seconds=120)
    task_id = _seed_task(
        Session,
        lease_seconds=-10,
        retries=1,
        cancellation_requested_at=cancel_time,
    )

    assert service.recover_expired_tasks() == 1

    task = _get_task(Session, task_id)
    assert task.status == "CANCELLED"
    assert task.retries == 1
    assert task.completed_at is not None
    assert task.claim_token is None
    assert task.worker_id is None
    assert task.lease_expires_at is None


def test_terminal_and_non_processing_tasks_are_preserved(ai_recovery_db):
    """Terminal and approval-pending tasks are never touched by recovery."""
    Session = ai_recovery_db["Session"]
    service = ai_recovery_db["service"]

    seeded = {
        "SUCCEEDED": _seed_task(Session, status="SUCCEEDED"),
        "FAILED": _seed_task(Session, status="FAILED", error_message="original"),
        "CANCELLED": _seed_task(Session, status="CANCELLED"),
        "AWAITING_APPROVAL": _seed_task(
            Session, status="AWAITING_APPROVAL", proposal_json={"category": "doc"}
        ),
        "PENDING": _seed_task(Session, status="PENDING"),
    }

    assert service.recover_expired_tasks() == 0

    for status, task_id in seeded.items():
        task = _get_task(Session, task_id)
        assert task.status == status
        assert task.retries == 0

    assert _get_task(Session, seeded["FAILED"]).error_message == "original"
    assert _get_task(Session, seeded["AWAITING_APPROVAL"]).proposal_json == {
        "category": "doc"
    }


# ---- Validation and failure handling ----


@pytest.mark.parametrize("batch_size", [0, -1, None, "10", True, 1.5])
def test_invalid_batch_size_rejected(ai_recovery_db, batch_size):
    """batch_size must be a positive integer."""
    service = ai_recovery_db["service"]
    with pytest.raises(ValueError, match="batch_size"):
        service.recover_expired_tasks(batch_size=batch_size)


def test_default_batch_size_constant():
    """The documented default batch size is a positive integer."""
    assert DEFAULT_RECOVERY_BATCH_SIZE == 10
    assert AiWorkerService.recover_expired_tasks.__defaults__ == (
        DEFAULT_RECOVERY_BATCH_SIZE,
    )


def test_database_factory_error_propagates(ai_recovery_db):
    """A failing session factory surfaces the error to the caller."""
    service = AiWorkerService(lambda: (_ for _ in ()).throw(RuntimeError("db down")))

    with pytest.raises(RuntimeError, match="db down"):
        service.recover_expired_tasks()


def test_transition_error_rolls_back(ai_recovery_db):
    """A failure inside the transition rolls back without partial changes."""
    Session = ai_recovery_db["Session"]
    service = ai_recovery_db["service"]
    task_id = _seed_task(Session, lease_seconds=-10, retries=1)

    def failing_transition(db, candidate_ids, now):
        raise RuntimeError("write failed")

    service._transition_expired_tasks = failing_transition

    with pytest.raises(RuntimeError, match="write failed"):
        service.recover_expired_tasks()

    task = _get_task(Session, task_id)
    assert task.status == "PROCESSING"
    assert task.retries == 1
    assert task.claim_token == "claim-token"
    assert task.worker_id == "worker-1"
    assert task.lease_expires_at is not None
