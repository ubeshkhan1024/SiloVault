"""Tests for AiWorkerService."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models.ai_task import AiTask
from backend.services.ai_worker import AiWorkerService


@pytest.fixture
def ai_worker_db():
    """Provides an isolated SQLite database engine, session factory, and AiWorkerService."""
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


def test_claim_pending_task_success(ai_worker_db):
    """An eligible PENDING task is claimed successfully."""
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    # Seed a PENDING task
    task = AiTask(
        task_type="text_classification",
        status="PENDING",
    )
    db = Session()
    db.add(task)
    db.commit()
    db.refresh(task)
    db.close()

    # Claim the task
    worker_id = "worker-1"
    claimed = service.claim_one_pending_task(worker_id)

    assert claimed is not None
    assert claimed.id == task.id
    assert claimed.status == "PROCESSING"
    assert claimed.claim_token is not None
    assert claimed.worker_id == worker_id
    assert claimed.started_at is not None
    assert claimed.lease_expires_at is not None

    # Verify lease is approximately 5 minutes in the future
    now = datetime.now(timezone.utc)
    expected_lease = now + timedelta(seconds=300)
    # Allow 1 second tolerance for test execution time
    assert abs((claimed.lease_expires_at - expected_lease).total_seconds()) < 1


def test_claim_returns_none_when_no_pending_tasks(ai_worker_db):
    """No pending task returns None."""
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    # No tasks seeded
    claimed = service.claim_one_pending_task("worker-1")
    assert claimed is None


def test_claim_skips_non_pending_tasks(ai_worker_db):
    """CANCELLED, FAILED, SUCCEEDED, and PROCESSING tasks cannot be claimed."""
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    # Seed tasks in various non-PENDING states
    statuses = ["CANCELLED", "FAILED", "SUCCEEDED", "PROCESSING"]
    tasks = []
    for status in statuses:
        task = AiTask(
            task_type="text_classification",
            status=status,
            claim_token=None,
            worker_id=None,
        )
        tasks.append(task)

    db = Session()
    db.add_all(tasks)
    db.commit()
    for task in tasks:
        db.refresh(task)
    db.close()

    # Attempt to claim - should return None
    claimed = service.claim_one_pending_task("worker-1")
    assert claimed is None


def test_sequential_workers_claim_different_tasks(ai_worker_db):
    """Sequential workers claim different tasks rather than the same task."""
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    # Seed two PENDING tasks
    tasks = []
    for i in range(2):
        task = AiTask(
            task_type="text_classification",
            status="PENDING",
        )
        tasks.append(task)

    db = Session()
    db.add_all(tasks)
    db.commit()
    for task in tasks:
        db.refresh(task)
    db.close()

    # Worker A claims the first task
    task_a = service.claim_one_pending_task("worker-a")
    assert task_a is not None

    # Worker B claims the second task (not the same one)
    task_b = service.claim_one_pending_task("worker-b")
    assert task_b is not None
    assert task_b.id != task_a.id


def test_claim_race_condition_returns_none(ai_worker_db):
    """A task cancellation race cannot accidentally claim a cancelled task."""
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    # Seed a PENDING task
    task = AiTask(
        task_type="text_classification",
        status="PENDING",
    )
    db = Session()
    db.add(task)
    db.commit()
    db.refresh(task)
    db.close()

    # Manually update the task to CANCELLED (simulating a concurrent cancellation)
    db = Session()
    stmt = (
        update(AiTask)
        .where(AiTask.id == task.id)
        .values(status="CANCELLED")
    )
    db.execute(stmt)
    db.commit()
    db.close()

    # Attempt to claim - should return None because task is no longer PENDING
    claimed = service.claim_one_pending_task("worker-1")
    assert claimed is None


def test_claim_validates_worker_id(ai_worker_db):
    """Worker ID validation raises ValueError for empty or None IDs."""
    service = ai_worker_db["service"]

    with pytest.raises(ValueError, match="worker_id must be a non-empty string"):
        service.claim_one_pending_task("")

    with pytest.raises(ValueError, match="worker_id must be a non-empty string"):
        service.claim_one_pending_task(None)


# ---- Lease Renewal Tests ----

def test_renew_task_lease_success(ai_worker_db):
    """A valid lease renewal extends the lease by approximately 300 seconds."""
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    # Seed a PROCESSING task with a lease
    task = AiTask(
        task_type="text_classification",
        status="PROCESSING",
        claim_token="original-token",
        worker_id="worker-1",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
    )
    db = Session()
    db.add(task)
    db.commit()
    db.refresh(task)
    db.close()

    # Renew the lease
    renewed = service.renew_task_lease(task.id, "worker-1", "original-token")

    assert renewed is True

    # Verify the lease was extended
    db = Session()
    try:
        refreshed = db.get(AiTask, task.id)
        assert refreshed is not None
        assert refreshed.lease_expires_at > datetime.now(timezone.utc) + timedelta(seconds=200)
        # Should be approximately now + 300 seconds
        expected = datetime.now(timezone.utc) + timedelta(seconds=300)
        assert abs((refreshed.lease_expires_at - expected).total_seconds()) < 5
    finally:
        db.close()


def test_renew_task_lease_wrong_worker(ai_worker_db):
    """Wrong worker ID returns False and leaves expiry unchanged."""
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    # Seed a PROCESSING task
    task = AiTask(
        task_type="text_classification",
        status="PROCESSING",
        claim_token="original-token",
        worker_id="worker-1",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
    )
    db = Session()
    db.add(task)
    db.commit()
    db.refresh(task)
    db.close()

    # Try with wrong worker ID
    renewed = service.renew_task_lease(task.id, "worker-2", "original-token")

    assert renewed is False

    # Verify the lease was NOT modified
    db = Session()
    try:
        refreshed = db.get(AiTask, task.id)
        assert refreshed is not None
        assert refreshed.lease_expires_at > datetime.now(timezone.utc) + timedelta(seconds=200)
    finally:
        db.close()


def test_renew_task_lease_wrong_token(ai_worker_db):
    """Wrong claim token returns False and leaves expiry unchanged."""
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    # Seed a PROCESSING task
    task = AiTask(
        task_type="text_classification",
        status="PROCESSING",
        claim_token="original-token",
        worker_id="worker-1",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
    )
    db = Session()
    db.add(task)
    db.commit()
    db.refresh(task)
    db.close()

    # Try with wrong claim token
    renewed = service.renew_task_lease(task.id, "worker-1", "wrong-token")

    assert renewed is False

    # Verify the lease was NOT modified
    db = Session()
    try:
        refreshed = db.get(AiTask, task.id)
        assert refreshed is not None
        assert refreshed.lease_expires_at > datetime.now(timezone.utc) + timedelta(seconds=200)
    finally:
        db.close()



def test_renew_task_lease_expired_lease(ai_worker_db):
    """Expired lease returns False and leaves expiry unchanged."""
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    # Seed a PROCESSING task with an expired lease
    past_time = datetime.now(timezone.utc) - timedelta(seconds=60)
    task = AiTask(
        task_type="text_classification",
        status="PROCESSING",
        claim_token="original-token",
        worker_id="worker-1",
        lease_expires_at=past_time,
    )
    db = Session()
    db.add(task)
    db.commit()
    db.refresh(task)
    db.close()

    # Try to renew with an expired lease
    renewed = service.renew_task_lease(task.id, "worker-1", "original-token")

    assert renewed is False

    # Verify the lease was NOT modified and remains expired
    db = Session()
    try:
        refreshed = db.get(AiTask, task.id)
        assert refreshed is not None
        assert refreshed.lease_expires_at <= datetime.now(timezone.utc)
    finally:
        db.close()


def test_renew_task_lease_missing_task(ai_worker_db):
    """Missing task returns False."""
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    # Try to renew a non-existent task
    renewed = service.renew_task_lease("non-existent-id", "worker-1", "original-token")

    assert renewed is False


def test_renew_task_lease_non_processing(ai_worker_db):
    """Non-PROCESSING task returns False and leaves expiry unchanged."""
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    # Seed a PENDING task (cannot be renewed)
    task = AiTask(
        task_type="text_classification",
        status="PENDING",
        claim_token="original-token",
        worker_id="worker-1",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
    )
    db = Session()
    db.add(task)
    db.commit()
    db.refresh(task)
    db.close()

    # Try to renew a PENDING task
    renewed = service.renew_task_lease(task.id, "worker-1", "original-token")

    assert renewed is False

    # Verify the lease was NOT modified
    db = Session()
    try:
        refreshed = db.get(AiTask, task.id)
        assert refreshed is not None
        assert refreshed.lease_expires_at > datetime.now(timezone.utc) + timedelta(seconds=200)
    finally:
        db.close()


def test_renewal_no_modification_on_failure(ai_worker_db):
    """Failed renewal preserves all relevant task ownership and lease fields."""
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    # Seed a PROCESSING task with a specific lease
    original_lease = datetime.now(timezone.utc) + timedelta(seconds=300)
    task = AiTask(
        task_type="text_classification",
        status="PROCESSING",
        claim_token="original-token",
        worker_id="worker-1",
        lease_expires_at=original_lease,
    )
    db = Session()
    db.add(task)
    db.commit()
    db.refresh(task)
    db.close()

    # Attempt renewal with wrong token (should fail)
    renewed = service.renew_task_lease(task.id, "worker-1", "wrong-token")

    assert renewed is False

    # Verify absolutely nothing was changed
    db = Session()
    try:
        refreshed = db.get(AiTask, task.id)
        assert refreshed is not None
        assert refreshed.status == "PROCESSING"
        assert refreshed.claim_token == "original-token"
        assert refreshed.worker_id == "worker-1"
        assert refreshed.lease_expires_at == original_lease
    finally:
        db.close()


def test_older_task_claimed_first(ai_worker_db):
    """When two pending tasks have different created_at values, the older one is claimed first."""
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    # Create two PENDING tasks - older first
    older_task = AiTask(
        task_type="text_classification",
        status="PENDING",
    )
    newer_task = AiTask(
        task_type="text_classification",
        status="PENDING",
    )

    db = Session()
    db.add_all([older_task, newer_task])
    db.commit()
    db.refresh(older_task)
    db.refresh(newer_task)
    db.close()

    # Claim should return the older task first (ordering by created_at ASC)
    claimed = service.claim_one_pending_task("worker-1")
    assert claimed is not None
    assert claimed.id == older_task.id


def test_whitespace_only_worker_id_rejected(ai_worker_db):
    """Whitespace-only worker IDs are rejected."""
    service = ai_worker_db["service"]

    with pytest.raises(ValueError, match="worker_id must be a non-empty string"):
        service.claim_one_pending_task("   ")


def test_worker_id_exceeds_max_length_rejected(ai_worker_db):
    """Worker IDs exceeding the model's maximum length are rejected."""
    service = ai_worker_db["service"]

    long_id = "a" * 65  # 65 characters, exceeds 64 max
    with pytest.raises(ValueError, match="worker_id must not exceed 64 characters"):
        service.claim_one_pending_task(long_id)


def test_non_string_worker_id_rejected(ai_worker_db):
    """Non-string worker IDs are rejected."""
    service = ai_worker_db["service"]

    with pytest.raises(ValueError, match="worker_id must be a non-empty string"):
        service.claim_one_pending_task(123)  # int, not string

    with pytest.raises(ValueError, match="worker_id must be a non-empty string"):
        service.claim_one_pending_task(True)  # bool, not string


def test_returned_task_readable_after_return(ai_worker_db):
    """The returned AiTask remains safely readable after the service commits and closes its session."""
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    # Seed a PENDING task
    task = AiTask(
        task_type="text_classification",
        status="PENDING",
    )
    db = Session()
    db.add(task)
    db.commit()
    db.refresh(task)
    db.close()

    # Claim the task
    claimed = service.claim_one_pending_task("worker-1")
    assert claimed is not None
    assert claimed.status == "PROCESSING"
    assert claimed.claim_token is not None
    assert claimed.worker_id == "worker-1"
    assert claimed.started_at is not None
    assert claimed.lease_expires_at is not None

    # Verify the returned task's fields are accessible (detached entity)
    # The task should still be readable even though the service session is closed
    assert claimed.id == task.id
    assert claimed.task_type == "text_classification"



# ---- M4.1 B3: Processor Registry Tests ----

def test_register_processor_success(ai_worker_db):
    service = ai_worker_db["service"]

    def processor(task_id: str) -> dict:
        return {}

    service.register_processor("text_classification", processor)

    assert "text_classification" in service.get_registered_task_types()
    assert service._processors["text_classification"] is processor


def test_register_processor_whitespace_rejected(ai_worker_db):
    service = ai_worker_db["service"]

    with pytest.raises(ValueError):
        service.register_processor("   ", lambda task_id: {})


def test_unregister_processor(ai_worker_db):
    service = ai_worker_db["service"]
    service.register_processor("text_classification", lambda task_id: {})

    service.unregister_processor("text_classification")

    assert "text_classification" not in service.get_registered_task_types()


def test_get_registered_task_types(ai_worker_db):
    service = ai_worker_db["service"]
    service.register_processor("text_classification", lambda task_id: {})
    service.register_processor("metadata_extraction", lambda task_id: {})

    registered = service.get_registered_task_types()

    assert set(registered) == {"text_classification", "metadata_extraction"}

    # Returned list must not expose the internal registry.
    registered.clear()
    assert len(service.get_registered_task_types()) == 2


def test_claim_one_pending_task_by_type_registered(ai_worker_db):
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]
    service.register_processor("text_classification", lambda task_id: {})

    task = AiTask(task_type="text_classification", status="PENDING")
    db = Session()
    db.add(task)
    db.commit()
    db.refresh(task)
    task_id = task.id
    db.close()

    claimed = service.claim_one_pending_task_by_type(
        "worker-1", "text_classification"
    )

    assert claimed is not None
    assert claimed.id == task_id
    assert claimed.status == "PROCESSING"
    assert claimed.worker_id == "worker-1"
    assert claimed.claim_token is not None
    assert claimed.lease_expires_at is not None


def test_claim_one_pending_task_by_type_unregistered(ai_worker_db):
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    task = AiTask(task_type="text_classification", status="PENDING")
    db = Session()
    db.add(task)
    db.commit()
    db.close()

    claimed = service.claim_one_pending_task_by_type(
        "worker-1", "text_classification"
    )

    assert claimed is None


def test_claim_one_pending_task_by_type_blocks_unsupported(ai_worker_db):
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    service.register_processor("metadata_extraction", lambda task_id: {})

    unsupported = AiTask(task_type="text_classification", status="PENDING")
    supported = AiTask(task_type="metadata_extraction", status="PENDING")

    db = Session()
    db.add_all([unsupported, supported])
    db.commit()
    db.refresh(supported)
    supported_id = supported.id
    db.close()

    claimed = service.claim_one_pending_task_by_type(
        "worker-1", "metadata_extraction"
    )

    assert claimed is not None
    assert claimed.id == supported_id
    assert claimed.task_type == "metadata_extraction"


def test_claim_by_type_rejects_invalid_task_type(ai_worker_db):
    service = ai_worker_db["service"]

    with pytest.raises(ValueError):
        service.claim_one_pending_task_by_type("worker-1", "")

    with pytest.raises(ValueError):
        service.claim_one_pending_task_by_type("worker-1", "   ")

    with pytest.raises(ValueError):
        service.claim_one_pending_task_by_type("worker-1", None)




def test_process_one_task_success(ai_worker_db):
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    task = AiTask(
        task_type="text_classification",
        status="PENDING",
    )

    db = Session()
    db.add(task)
    db.commit()
    db.refresh(task)
    task_id = task.id
    db.close()

    processed_ids = []

    def processor(received_task_id):
        processed_ids.append(received_task_id)
        return {
            "category": "document",
            "confidence": 0.95,
        }

    service.register_processor("text_classification", processor)

    result = service.process_one_task(
        "worker-1",
        "text_classification",
    )

    assert result is True
    assert processed_ids == [task_id]

    db = Session()
    try:
        updated_task = db.get(AiTask, task_id)

        assert updated_task.status == "AWAITING_APPROVAL"
        assert updated_task.proposal_json == {
            "category": "document",
            "confidence": 0.95,
        }
        assert updated_task.completed_at is not None
        assert updated_task.claim_token is None
        assert updated_task.worker_id is None
        assert updated_task.lease_expires_at is None
    finally:
        db.close()



def test_process_one_task_processor_failure(ai_worker_db):
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    task = AiTask(
        task_type="text_classification",
        status="PENDING",
    )

    db = Session()
    db.add(task)
    db.commit()
    db.refresh(task)
    task_id = task.id
    db.close()

    def failing_processor(received_task_id):
        raise RuntimeError("Test processor failure")

    service.register_processor(
        "text_classification",
        failing_processor,
    )

    result = service.process_one_task(
        "worker-1",
        "text_classification",
    )

    assert result is True

    db = Session()
    try:
        updated_task = db.get(AiTask, task_id)

        assert updated_task.status == "FAILED"
        assert updated_task.error_message == "Test processor failure"
        assert updated_task.completed_at is not None
        assert updated_task.claim_token is None
        assert updated_task.worker_id is None
        assert updated_task.lease_expires_at is None
    finally:
        db.close()



def test_process_one_task_cancellation_during_processing(ai_worker_db):
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    task = AiTask(
        task_type="text_classification",
        status="PENDING",
    )

    db = Session()
    db.add(task)
    db.commit()
    db.refresh(task)
    task_id = task.id
    db.close()

    def cancelling_processor(received_task_id):
        cancel_db = Session()
        try:
            cancel_db.query(AiTask).filter(
                AiTask.id == received_task_id
            ).update({
                "cancellation_requested_at": datetime.now(timezone.utc)
            })
            cancel_db.commit()
        finally:
            cancel_db.close()

        return {"category": "document"}

    service.register_processor(
        "text_classification",
        cancelling_processor,
    )

    result = service.process_one_task(
        "worker-1",
        "text_classification",
    )

    assert result is True

    db = Session()
    try:
        updated_task = db.get(AiTask, task_id)

        assert updated_task.status == "CANCELLED"
        assert updated_task.proposal_json is None
        assert updated_task.completed_at is not None
        assert updated_task.claim_token is None
        assert updated_task.worker_id is None
        assert updated_task.lease_expires_at is None
    finally:
        db.close()


def test_process_one_task_invalid_processor_result(ai_worker_db):
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    db = Session()
    task = AiTask(task_type="text_classification", status="PENDING")
    db.add(task)
    db.commit()
    task_id = task.id
    db.close()

    service.register_processor("text_classification", lambda task_id: ["invalid"])

    result = service.process_one_task("worker-1", "text_classification")

    assert result is True

    db = Session()
    try:
        updated = db.get(AiTask, task_id)
        assert updated.status == "FAILED"
        assert updated.proposal_json is None
    finally:
        db.close()


def test_process_one_task_non_json_serializable_result(ai_worker_db):
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    db = Session()
    task = AiTask(task_type="text_classification", status="PENDING")
    db.add(task)
    db.commit()
    task_id = task.id
    db.close()

    service.register_processor(
        "text_classification",
        lambda task_id: {"invalid": object()},
    )

    result = service.process_one_task("worker-1", "text_classification")

    assert result is True

    db = Session()
    try:
        updated = db.get(AiTask, task_id)
        assert updated.status == "FAILED"
        assert updated.proposal_json is None
    finally:
        db.close()


def test_process_one_task_unregistered_processor(ai_worker_db):
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    db = Session()
    task = AiTask(task_type="text_classification", status="PENDING")
    db.add(task)
    db.commit()
    task_id = task.id
    db.close()

    result = service.process_one_task("worker-1", "text_classification")

    assert result is False

    db = Session()
    try:
        updated = db.get(AiTask, task_id)
        assert updated.status == "PENDING"
        assert updated.worker_id is None
        assert updated.claim_token is None
    finally:
        db.close()


def test_process_one_task_expired_lease_cannot_finalize(ai_worker_db):
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    db = Session()
    task = AiTask(task_type="text_classification", status="PENDING")
    db.add(task)
    db.commit()
    task_id = task.id
    db.close()

    def expire_lease(received_task_id):
        update_db = Session()
        try:
            update_db.query(AiTask).filter(
                AiTask.id == received_task_id
            ).update({
                "lease_expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc)
            })
            update_db.commit()
        finally:
            update_db.close()

        return {"category": "document"}

    service.register_processor("text_classification", expire_lease)

    result = service.process_one_task("worker-1", "text_classification")

    assert result is False

    db = Session()
    try:
        updated = db.get(AiTask, task_id)
        assert updated.status == "PROCESSING"
        assert updated.proposal_json is None
    finally:
        db.close()


def test_process_one_task_cancellation_requested_before_execution(ai_worker_db):
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]

    db = Session()
    task = AiTask(
        task_type="text_classification",
        status="PENDING",
        cancellation_requested_at=datetime.now(timezone.utc),
    )
    db.add(task)
    db.commit()
    task_id = task.id
    db.close()

    def should_not_run(received_task_id):
        raise AssertionError("Cancelled task processor must not execute")

    service.register_processor("text_classification", should_not_run)

    result = service.process_one_task("worker-1", "text_classification")

    assert result is True

    db = Session()
    try:
        updated = db.get(AiTask, task_id)
        assert updated.status == "CANCELLED"
        assert updated.proposal_json is None
        assert updated.completed_at is not None
        assert updated.claim_token is None
        assert updated.worker_id is None
        assert updated.lease_expires_at is None
    finally:
        db.close()

# ---- Lease Heartbeat Tests ----


def _seed_pending_task(Session) -> str:
    """Seed a single PENDING text classification task and return its ID."""
    db = Session()
    try:
        task = AiTask(task_type="text_classification", status="PENDING")
        db.add(task)
        db.commit()
        return task.id
    finally:
        db.close()


def test_heartbeat_renews_lease_during_long_running_processor(ai_worker_db):
    """A processor that outlives the heartbeat interval keeps its lease."""
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]
    service._lease_heartbeat_interval = 0.01

    original_renew = service.renew_task_lease
    renewal_results = []
    renewed = threading.Event()

    def tracking_renew(task_id, worker_id, claim_token):
        result = original_renew(task_id, worker_id, claim_token)
        renewal_results.append(result)
        renewed.set()
        return result

    service.renew_task_lease = tracking_renew
    task_id = _seed_pending_task(Session)

    observed = {}

    def slow_processor(received_task_id):
        # Deterministic: only continue once the heartbeat renewed the lease.
        assert renewed.wait(timeout=5), "heartbeat did not renew the lease"

        db = Session()
        try:
            task = db.get(AiTask, received_task_id)
            observed["status"] = task.status
            observed["lease_expires_at"] = task.lease_expires_at
        finally:
            db.close()

        return {"category": "document"}

    service.register_processor("text_classification", slow_processor)

    result = service.process_one_task("worker-1", "text_classification")

    assert result is True
    assert renewal_results and all(renewal_results)
    # The renewal happened while the task was still being processed and
    # extended the lease well beyond the original claim window.
    assert observed["status"] == "PROCESSING"
    assert observed["lease_expires_at"] > datetime.now(timezone.utc) + timedelta(
        seconds=250
    )

    db = Session()
    try:
        task = db.get(AiTask, task_id)
        assert task.status == "AWAITING_APPROVAL"
        assert task.proposal_json == {"category": "document"}
    finally:
        db.close()


def test_heartbeat_thread_stops_after_processing_completes(ai_worker_db):
    """The heartbeat thread is joined before processing returns."""
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]
    service._lease_heartbeat_interval = 0.01

    original_renew = service.renew_task_lease
    renewal_threads = []
    renewed = threading.Event()

    def tracking_renew(task_id, worker_id, claim_token):
        renewal_threads.append(threading.current_thread())
        result = original_renew(task_id, worker_id, claim_token)
        renewed.set()
        return result

    service.renew_task_lease = tracking_renew
    _seed_pending_task(Session)

    def processor(received_task_id):
        assert renewed.wait(timeout=5)
        return {"category": "document"}

    service.register_processor("text_classification", processor)

    assert service.process_one_task("worker-1", "text_classification") is True

    assert renewal_threads
    heartbeat_thread = renewal_threads[0]
    assert heartbeat_thread is not threading.main_thread()
    assert heartbeat_thread.daemon is True
    # stop() joined the thread before the task was finalized.
    assert heartbeat_thread.is_alive() is False


def test_lost_ownership_prevents_finalization(ai_worker_db):
    """A renewal returning False marks ownership lost and skips finalization."""
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]
    service._lease_heartbeat_interval = 0.01

    original_renew = service.renew_task_lease
    loss_detected = threading.Event()
    renewal_threads = []

    def tracking_renew(task_id, worker_id, claim_token):
        renewal_threads.append(threading.current_thread())
        result = original_renew(task_id, worker_id, claim_token)
        if result is False:
            loss_detected.set()
        return result

    service.renew_task_lease = tracking_renew
    task_id = _seed_pending_task(Session)

    def stealing_processor(received_task_id):
        # Simulate another worker taking over the task mid-processing.
        db = Session()
        try:
            db.query(AiTask).filter(AiTask.id == received_task_id).update(
                {
                    "worker_id": "worker-2",
                    "claim_token": "other-token",
                }
            )
            db.commit()
        finally:
            db.close()

        assert loss_detected.wait(timeout=5), "heartbeat did not detect lost ownership"
        return {"category": "document"}

    service.register_processor("text_classification", stealing_processor)

    assert service.process_one_task("worker-1", "text_classification") is False

    # The heartbeat, not just the final ownership check, detected the loss.
    assert loss_detected.is_set()
    assert renewal_threads and not renewal_threads[-1].is_alive()

    db = Session()
    try:
        task = db.get(AiTask, task_id)
        assert task.status == "PROCESSING"
        assert task.worker_id == "worker-2"
        assert task.claim_token == "other-token"
        assert task.proposal_json is None
        assert task.error_message is None
        assert task.completed_at is None
        assert task.lease_expires_at is not None
    finally:
        db.close()


def test_renewal_database_failure_prevents_finalization(ai_worker_db):
    """A renewal that raises fails closed and the task is not finalized."""
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]
    service._lease_heartbeat_interval = 0.01

    renewal_attempted = threading.Event()
    renewal_threads = []

    def failing_renew(task_id, worker_id, claim_token):
        renewal_threads.append(threading.current_thread())
        renewal_attempted.set()
        raise RuntimeError("database unavailable")

    service.renew_task_lease = failing_renew
    task_id = _seed_pending_task(Session)

    def processor(received_task_id):
        assert renewal_attempted.wait(timeout=5), "heartbeat did not attempt renewal"
        return {"category": "document"}

    service.register_processor("text_classification", processor)

    assert service.process_one_task("worker-1", "text_classification") is False

    assert renewal_threads and not renewal_threads[0].is_alive()

    db = Session()
    try:
        task = db.get(AiTask, task_id)
        assert task.status == "PROCESSING"
        assert task.worker_id == "worker-1"
        assert task.claim_token is not None
        assert task.proposal_json is None
        assert task.error_message is None
        assert task.completed_at is None
    finally:
        db.close()


def test_processor_exception_stops_and_joins_heartbeat(ai_worker_db):
    """A raising processor still stops and joins the heartbeat thread."""
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]
    service._lease_heartbeat_interval = 0.01

    original_renew = service.renew_task_lease
    renewal_threads = []
    renewed = threading.Event()

    def tracking_renew(task_id, worker_id, claim_token):
        renewal_threads.append(threading.current_thread())
        result = original_renew(task_id, worker_id, claim_token)
        renewed.set()
        return result

    service.renew_task_lease = tracking_renew
    task_id = _seed_pending_task(Session)

    def failing_processor(received_task_id):
        assert renewed.wait(timeout=5)
        raise RuntimeError("boom")

    service.register_processor("text_classification", failing_processor)

    assert service.process_one_task("worker-1", "text_classification") is True

    assert renewal_threads and not renewal_threads[0].is_alive()

    db = Session()
    try:
        task = db.get(AiTask, task_id)
        assert task.status == "FAILED"
        assert task.error_message == "boom"
        assert task.claim_token is None
        assert task.worker_id is None
        assert task.lease_expires_at is None
    finally:
        db.close()


def test_cancellation_during_processing_wins_with_heartbeat(ai_worker_db):
    """Cancellation still takes priority while the lease is being renewed."""
    Session = ai_worker_db["Session"]
    service = ai_worker_db["service"]
    service._lease_heartbeat_interval = 0.01

    original_renew = service.renew_task_lease
    renewed = threading.Event()

    def tracking_renew(task_id, worker_id, claim_token):
        result = original_renew(task_id, worker_id, claim_token)
        renewed.set()
        return result

    service.renew_task_lease = tracking_renew
    task_id = _seed_pending_task(Session)

    def cancelling_processor(received_task_id):
        db = Session()
        try:
            db.query(AiTask).filter(AiTask.id == received_task_id).update(
                {"cancellation_requested_at": datetime.now(timezone.utc)}
            )
            db.commit()
        finally:
            db.close()

        assert renewed.wait(timeout=5)
        return {"category": "document"}

    service.register_processor("text_classification", cancelling_processor)

    assert service.process_one_task("worker-1", "text_classification") is True

    db = Session()
    try:
        task = db.get(AiTask, task_id)
        assert task.status == "CANCELLED"
        assert task.proposal_json is None
        assert task.completed_at is not None
        assert task.claim_token is None
        assert task.worker_id is None
        assert task.lease_expires_at is None
    finally:
        db.close()


@pytest.mark.parametrize(
    "interval",
    [0, -1, float("inf"), float("nan"), None, "fast", True],
)
def test_invalid_lease_heartbeat_interval_rejected(ai_worker_db, interval):
    """The heartbeat interval must be a positive finite number."""
    with pytest.raises(ValueError):
        AiWorkerService(
            ai_worker_db["Session"],
            lease_heartbeat_interval=interval,
        )
