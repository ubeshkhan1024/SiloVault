"""Focused tests for the AI task approval service (M4.1 B3.3b)."""

import pytest
import re
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models.ai_task import AiTask
from backend.models.unified_file import UnifiedFileRecord
from backend.services.approval import ApprovalService, TaskNotFoundError, InvalidTaskStateError


@pytest.fixture
def approval_db():
    """Provides an isolated SQLite database engine, session factory, and ApprovalService."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    service = ApprovalService(TestSession)

    yield {
        "engine": engine,
        "Session": TestSession,
        "service": service,
    }

    engine.dispose()


def _make_file(session, name: str = "doc.txt") -> UnifiedFileRecord:
    record = UnifiedFileRecord(
        provider="LOCAL",
        provider_account_id=None,
        provider_file_id=name,
        name=name,
        logical_path=f"/{name}",
        mime_type="text/plain",
        size_bytes=42,
        sync_state="LOCAL",
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


def _make_awaiting_approval_task(session, file_id: str) -> AiTask:
    task = AiTask(
        file_id=file_id,
        task_type="text_classification",
        status="AWAITING_APPROVAL",
        proposal_json={"category": "document", "confidence": 0.95},
        claim_token="old_token",
        worker_id="old_worker",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def _make_processing_task(session, file_id: str) -> AiTask:
    task = AiTask(
        file_id=file_id,
        task_type="text_classification",
        status="PROCESSING",
        claim_token="worker_token",
        worker_id="worker-1",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return task



def test_approve_task_success(approval_db):
    """Approving an AWAITING_APPROVAL task transitions to SUCCEEDED with decision recorded."""
    Session = approval_db["Session"]
    service = approval_db["service"]
    engine = approval_db["engine"]

    db = Session()
    try:
        file = _make_file(db)
        file_id = file.id
        task = _make_awaiting_approval_task(db, file_id)
        task_id = task.id
    finally:
        db.close()

    updated = service.approve_task(task_id)

    db = Session()
    try:
        assert updated.status == "SUCCEEDED"
        assert updated.decision == "APPROVE"
        assert updated.decided_at is not None
        assert updated.completed_at is not None
        assert updated.claim_token is None
        assert updated.worker_id is None
        assert updated.lease_expires_at is None
        assert updated.proposal_json == {
            "category": "document",
            "confidence": 0.95,
        }

        # Ensure the proposal JSON is preserved and file metadata untouched
        file_record = db.get(UnifiedFileRecord, file_id)
        assert file_record is not None
        assert file_record.category is None
        assert file_record.tags == []
    finally:
        db.close()


def test_reject_task_success(approval_db):
    """Rejecting an AWAITING_APPROVAL task transitions to REJECTED with decision recorded."""
    Session = approval_db["Session"]
    service = approval_db["service"]
    engine = approval_db["engine"]

    db = Session()
    try:
        file = _make_file(db)
        file_id = file.id
        task = _make_awaiting_approval_task(db, file_id)
        task_id = task.id
    finally:
        db.close()

    updated = service.reject_task(task_id)

    db = Session()
    try:
        assert updated.status == "REJECTED"
        assert updated.decision == "REJECT"
        assert updated.decided_at is not None
        assert updated.completed_at is not None
        assert updated.claim_token is None
        assert updated.worker_id is None
        assert updated.lease_expires_at is None
        assert updated.proposal_json == {"category": "document", "confidence": 0.95}

        # Ensure the proposal JSON is preserved and file metadata untouched
        file_record = db.get(UnifiedFileRecord, file_id)
        assert file_record.category is None
        assert file_record.tags == []
    finally:
        db.close()


def test_approve_task_not_found_raises_value_error(approval_db):
    """Approving a non-existent task raises TaskNotFoundError."""
    Session = approval_db["Session"]
    service = approval_db["service"]
    engine = approval_db["engine"]

    with pytest.raises(TaskNotFoundError, match="AI task 'nonexistent' was not found"):
        service.approve_task("nonexistent")


def test_reject_task_not_found_raises_value_error(approval_db):
    """Rejecting a non-existent task raises TaskNotFoundError."""
    Session = approval_db["Session"]
    service = approval_db["service"]
    engine = approval_db["engine"]

    with pytest.raises(TaskNotFoundError, match="AI task 'nonexistent' was not found"):
        service.reject_task("nonexistent")


def test_approve_task_wrong_status_raises_value_error(approval_db):
    """Approving a task not in AWAITING_APPROVAL state raises InvalidTaskStateError."""
    Session = approval_db["Session"]
    service = approval_db["service"]
    engine = approval_db["engine"]

    db = Session()
    try:
        file = _make_file(db)
        task = _make_processing_task(db, file.id)
        task_id = task.id
        task_status = task.status
    finally:
        db.close()

    service = ApprovalService(Session)
    with pytest.raises(
        InvalidTaskStateError, match=re.escape(f"Task is not in AWAITING_APPROVAL state (current: {task_status})")
    ):
        service.approve_task(task_id)

    # Ensure task remains unchanged
    db = Session()
    try:
        fresh = db.get(AiTask, task_id)
        assert fresh.status == "PROCESSING"
        assert fresh.claim_token == "worker_token"
        assert fresh.worker_id == "worker-1"
    finally:
        db.close()


def test_reject_task_wrong_status_raises_value_error(approval_db):
    """Rejecting a task not in AWAITING_APPROVAL state raises InvalidTaskStateError."""
    Session = approval_db["Session"]
    service = approval_db["service"]
    engine = approval_db["engine"]

    db = Session()
    try:
        file = _make_file(db)
        task = _make_processing_task(db, file.id)
        task_id = task.id
        task_status = task.status
    finally:
        db.close()

    service = ApprovalService(Session)
    with pytest.raises(
        InvalidTaskStateError, match=re.escape(f"Task is not in AWAITING_APPROVAL state (current: {task_status})")
    ):
        service.reject_task(task_id)

    # Ensure task remains unchanged
    db = Session()
    try:
        fresh = db.get(AiTask, task_id)
        assert fresh.status == "PROCESSING"
        assert fresh.claim_token == "worker_token"
        assert fresh.worker_id == "worker-1"
    finally:
        db.close()


def test_approve_task_duplicate_is_idempotent(approval_db):
    """Approving an already-approved task raises ValueError (409 equivalent)."""
    Session = approval_db["Session"]
    service = approval_db["service"]
    engine = approval_db["engine"]

    db = Session()
    try:
        file = _make_file(db)
        file_id = file.id
        task = _make_awaiting_approval_task(db, file_id)
        task_id = task.id
    finally:
        db.close()

    service = ApprovalService(Session)

    # First approval succeeds
    updated = service.approve_task(task_id)
    assert updated.status == "SUCCEEDED"
    assert updated.decision == "APPROVE"

    # Second approval fails
    with pytest.raises(
        ValueError, match=re.escape(f"Task is not in AWAITING_APPROVAL state (current: SUCCEEDED)")
    ):
        service.approve_task(task_id)

    # Task remains SUCCEEDED
    db = Session()
    try:
        fresh = db.get(AiTask, task_id)
        assert fresh.status == "SUCCEEDED"
        assert fresh.decision == "APPROVE"
    finally:
        db.close()


def test_reject_task_duplicate_is_idempotent(approval_db):
    """Rejecting an already-rejected task raises ValueError (409 equivalent)."""
    Session = approval_db["Session"]
    service = approval_db["service"]
    engine = approval_db["engine"]

    db = Session()
    try:
        file = _make_file(db)
        file_id = file.id
        task = _make_awaiting_approval_task(db, file_id)
        task_id = task.id
    finally:
        db.close()

    service = ApprovalService(Session)

    # First rejection succeeds
    updated = service.reject_task(task_id)
    assert updated.status == "REJECTED"
    assert updated.decision == "REJECT"

    # Second rejection fails
    with pytest.raises(
        ValueError, match=re.escape(f"Task is not in AWAITING_APPROVAL state (current: REJECTED)")
    ):
        service.reject_task(task_id)

    # Task remains REJECTED
    db = Session()
    try:
        fresh = db.get(AiTask, task_id)
        assert fresh.status == "REJECTED"
        assert fresh.decision == "REJECT"
    finally:
        db.close()


def test_approve_task_preserves_proposal_json(approval_db):
    """Approval preserves the proposal JSON exactly as stored."""
    Session = approval_db["Session"]
    service = approval_db["service"]
    engine = approval_db["engine"]

    db = Session()
    try:
        file = _make_file(db)
        file_id = file.id
        task = _make_awaiting_approval_task(db, file_id)
        task_id = task.id
        proposal = {
            "category": "Financial",
            "tags": ["#receipt", "#2026"],
            "confidence": 0.92,
            "nested": {"key": "value"},
        }
        task.proposal_json = proposal
        db.commit()
    finally:
        db.close()

    service = ApprovalService(Session)
    updated = service.approve_task(task_id)

    assert updated.proposal_json == proposal
    assert updated.proposal_json is not proposal  # new object, not same reference


def test_reject_task_preserves_proposal_json(approval_db):
    """Rejection preserves the proposal JSON exactly as stored."""
    Session = approval_db["Session"]
    service = approval_db["service"]
    engine = approval_db["engine"]

    db = Session()
    try:
        file = _make_file(db)
        file_id = file.id
        task = _make_awaiting_approval_task(db, file_id)
        task_id = task.id
        proposal = {"category": "Financial", "tags": ["#receipt"], "confidence": 0.92}
        task.proposal_json = proposal
        db.commit()
    finally:
        db.close()

    service = ApprovalService(Session)
    updated = service.reject_task(task_id)

    assert updated.proposal_json == proposal
    assert updated.proposal_json is not proposal  # new object, not same reference


def test_approve_task_clears_ownership_fields(approval_db):
    """Approval clears all ownership and lease fields."""
    Session = approval_db["Session"]
    service = approval_db["service"]
    engine = approval_db["engine"]

    db = Session()
    try:
        file = _make_file(db)
        task = _make_awaiting_approval_task(db, file.id)
        task_id = task.id
    finally:
        db.close()

    service = ApprovalService(Session)
    updated = service.approve_task(task_id)

    assert updated.claim_token is None
    assert updated.worker_id is None
    assert updated.lease_expires_at is None
    assert updated.completed_at is not None


def test_reject_task_clears_ownership_fields(approval_db):
    """Rejection clears all ownership and lease fields."""
    Session = approval_db["Session"]
    service = approval_db["service"]
    engine = approval_db["engine"]

    db = Session()
    try:
        file = _make_file(db)
        task = _make_awaiting_approval_task(db, file.id)
        task_id = task.id
    finally:
        db.close()

    service = ApprovalService(Session)
    updated = service.reject_task(task_id)

    assert updated.claim_token is None
    assert updated.worker_id is None
    assert updated.lease_expires_at is None
    assert updated.completed_at is not None


def test_approve_task_invalid_task_id_raises_value_error(approval_db):
    """Invalid task_id values raise ValueError."""
    Session = approval_db["Session"]
    service = approval_db["service"]
    engine = approval_db["engine"]

    with pytest.raises(ValueError, match="task_id must be a non-empty string"):
        service.approve_task("")

    with pytest.raises(ValueError, match="task_id must be a non-empty string"):
        service.approve_task(None)  # type: ignore

    with pytest.raises(ValueError, match="task_id must be a non-empty string"):
        service.approve_task(123)  # type: ignore


def test_reject_task_invalid_task_id_raises_value_error(approval_db):
    """Invalid task_id values raise ValueError."""
    Session = approval_db["Session"]
    service = approval_db["service"]
    engine = approval_db["engine"]

    with pytest.raises(ValueError, match="task_id must be a non-empty string"):
        service.reject_task("")

    with pytest.raises(ValueError, match="task_id must be a non-empty string"):
        service.reject_task(None)  # type: ignore

    with pytest.raises(ValueError, match="task_id must be a non-empty string"):
        service.reject_task(123)  # type: ignore


def test_approve_task_duplicate_decision_safety(approval_db):
    """Sequential duplicate approval attempts: only one succeeds."""
    Session = approval_db["Session"]
    service = approval_db["service"]
    engine = approval_db["engine"]

    db = Session()
    try:
        file = _make_file(db)
        file_id = file.id
        task = _make_awaiting_approval_task(db, file_id)
        task_id = task.id
    finally:
        db.close()

    service = ApprovalService(Session)

    # First approval succeeds
    updated1 = service.approve_task(task_id)
    assert updated1.status == "SUCCEEDED"

    # Second attempt (approval or rejection) fails
    with pytest.raises(InvalidTaskStateError, match=re.escape("Task is not in AWAITING_APPROVAL state (current: SUCCEEDED)")):
        service.approve_task(task_id)

    with pytest.raises(InvalidTaskStateError, match=re.escape("Task is not in AWAITING_APPROVAL state (current: SUCCEEDED)")):
        service.reject_task(task_id)

    # Task remains SUCCEEDED
    db = Session()
    try:
        fresh = db.get(AiTask, task_id)
        assert fresh.status == "SUCCEEDED"
        assert fresh.decision == "APPROVE"
    finally:
        db.close()


def test_reject_task_duplicate_decision_safety(approval_db):
    """Sequential duplicate rejection attempts: only one succeeds."""
    Session = approval_db["Session"]
    service = approval_db["service"]
    engine = approval_db["engine"]

    db = Session()
    try:
        file = _make_file(db)
        file_id = file.id
        task = _make_awaiting_approval_task(db, file_id)
        task_id = task.id
    finally:
        db.close()

    service = ApprovalService(Session)

    # First rejection succeeds
    updated1 = service.reject_task(task_id)
    assert updated1.status == "REJECTED"

    # Second attempt (rejection or approval) fails
    with pytest.raises(InvalidTaskStateError, match=re.escape("Task is not in AWAITING_APPROVAL state (current: REJECTED)")):
        service.reject_task(task_id)

    with pytest.raises(InvalidTaskStateError, match=re.escape("Task is not in AWAITING_APPROVAL state (current: REJECTED)")):
        service.approve_task(task_id)

    # Task remains REJECTED
    db = Session()
    try:
        fresh = db.get(AiTask, task_id)
        assert fresh.status == "REJECTED"
        assert fresh.decision == "REJECT"
    finally:
        db.close()


def test_approve_task_preserves_file_metadata(approval_db):
    """Approval does not modify the original file metadata."""
    Session = approval_db["Session"]
    service = approval_db["service"]
    engine = approval_db["engine"]

    db = Session()
    try:
        file = _make_file(db)
        file_id = file.id
        task = _make_awaiting_approval_task(db, file_id)
        task_id = task.id

        # Set some file metadata
        file.category = "Financial"
        file.tags = ["#receipt", "#2026"]
        db.commit()
    finally:
        db.close()

    service = ApprovalService(Session)
    service.approve_task(task_id)

    # File metadata unchanged
    db = Session()
    try:
        fresh_file = db.get(UnifiedFileRecord, file_id)
        assert fresh_file.category == "Financial"
        assert fresh_file.tags == ["#receipt", "#2026"]
        assert fresh_file.name == "doc.txt"
        assert fresh_file.size_bytes == 42
    finally:
        db.close()


def test_reject_task_preserves_file_metadata(approval_db):
    """Rejection does not modify the original file metadata."""
    Session = approval_db["Session"]
    service = approval_db["service"]
    engine = approval_db["engine"]

    db = Session()
    try:
        file = _make_file(db)
        file_id = file.id
        task = _make_awaiting_approval_task(db, file_id)
        task_id = task.id

        # Set some file metadata
        file.category = "Financial"
        file.tags = ["#receipt", "#2026"]
        db.commit()
    finally:
        db.close()

    service = ApprovalService(Session)
    service.reject_task(task_id)

    # File metadata unchanged
    db = Session()
    try:
        fresh_file = db.get(UnifiedFileRecord, file_id)
        assert fresh_file.category == "Financial"
        assert fresh_file.tags == ["#receipt", "#2026"]
        assert fresh_file.name == "doc.txt"
        assert fresh_file.size_bytes == 42
    finally:
        db.close()
