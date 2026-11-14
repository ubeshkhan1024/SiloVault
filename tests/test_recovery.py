"""Comprehensive unit and integration tests for SiloVault RecoveryService.

Tests cover:
- Atomic claiming and lost-claim guard
- Lease expiration handling and re-claiming
- Exponential backoff retry scheduling
- Maximum retries transition to FAILED
- Permanent failures (authentication, permission errors)
- Missing provider resources handled as success
- Google Drive cleanup using mocked adapter
- Background worker lifecycle (startup, polling, shutdown)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models.pending_deletion import PendingDeletion
from backend.services.recovery import RecoveryService, LEASE_DURATION_SECONDS, MAX_RETRIES
from backend.storage.capabilities import CAPABILITY_MAP
from backend.storage.errors import (
    StorageAuthenticationError,
    StorageError,
    StorageNotFoundError,
    StoragePermissionError,
    StorageRateLimitError,
    StorageUnavailableError,
)
from backend.storage.interfaces import StorageOperations
from backend.storage.registry import register, remove, replace


# ---------------------------------------------------------------------------
# Test Fixtures & Fakes
# ---------------------------------------------------------------------------

class FakeAdapter(StorageOperations):
    """Dummy storage adapter for testing recovery service interactions."""

    def __init__(self):
        self.cleaned_ids: list[str] = []
        self.cleanup_exc: Exception | None = None

    def upload(self, file_data: bytes, name: str, logical_path: str):
        raise NotImplementedError

    def download(self, provider_file_id: str) -> bytes:
        raise NotImplementedError

    def quarantine(self, provider_file_id: str) -> str:
        raise NotImplementedError

    def restore(self, quarantine_id: str):
        raise NotImplementedError

    def rename(self, provider_file_id: str, new_name: str):
        raise NotImplementedError

    def move(self, provider_file_id: str, new_logical_path: str):
        raise NotImplementedError

    def cleanup(self, quarantine_id: str) -> None:
        if self.cleanup_exc:
            raise self.cleanup_exc
        self.cleaned_ids.append(quarantine_id)


@pytest.fixture
def recovery_db(tmp_path):
    """Provides an isolated SQLite database engine, session factory, and RecoveryService."""
    db_file = tmp_path / "test_recovery.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    service = RecoveryService(lambda: Session())

    yield {
        "engine": engine,
        "Session": Session,
        "service": service,
    }

    engine.dispose()


@pytest.fixture
def fake_local_adapter():
    """Registers a FakeAdapter under 'LOCAL' for testing."""
    adapter = FakeAdapter()
    register("LOCAL", adapter, CAPABILITY_MAP["LOCAL"])
    yield adapter
    try:
        remove("LOCAL")
    except Exception:
        pass


@pytest.fixture
def fake_gdrive_adapter():
    """Registers a FakeAdapter under 'GOOGLE_DRIVE' for testing."""
    adapter = FakeAdapter()
    register("GOOGLE_DRIVE", adapter, CAPABILITY_MAP["GOOGLE_DRIVE"])
    yield adapter
    try:
        remove("GOOGLE_DRIVE")
    except Exception:
        pass


def _seed_pending(
    Session,
    provider: str = "LOCAL",
    provider_file_id: str = "file-1",
    quarantine_id: str = "quar-1",
    file_name: str = "test.txt",
    status: str = "PENDING",
    retry_count: int = 0,
    claim_token: str | None = None,
    lease_expires_at: datetime | None = None,
    next_retry_at: datetime | None = None,
) -> str:
    """Helper to insert a PendingDeletion record directly into DB."""
    db = Session()
    try:
        pd = PendingDeletion(
            provider=provider,
            provider_account_id=None if provider == "LOCAL" else "acc-1",
            provider_file_id=provider_file_id,
            quarantine_id=quarantine_id,
            file_name_snapshot=file_name,
            status=status,
            retry_count=retry_count,
            claim_token=claim_token,
            lease_expires_at=lease_expires_at,
            next_retry_at=next_retry_at,
        )
        db.add(pd)
        db.commit()
        db.refresh(pd)
        return pd.id
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------

def test_successful_local_cleanup(recovery_db, fake_local_adapter):
    """Clean processing of a PENDING LOCAL record removes the record from DB."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    pd_id = _seed_pending(Session, provider="LOCAL", quarantine_id="q-local-1")

    count = service.process_pending_deletions()
    assert count == 1
    assert fake_local_adapter.cleaned_ids == ["q-local-1"]

    db = Session()
    try:
        record = db.get(PendingDeletion, pd_id)
        assert record is None
    finally:
        db.close()


def test_successful_gdrive_cleanup_mocked(recovery_db, fake_gdrive_adapter):
    """Clean processing of a PENDING GOOGLE_DRIVE record via mocked adapter."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    pd_id = _seed_pending(Session, provider="GOOGLE_DRIVE", quarantine_id="drive-q-99")

    count = service.process_pending_deletions()
    assert count == 1
    assert fake_gdrive_adapter.cleaned_ids == ["drive-q-99"]

    db = Session()
    try:
        assert db.get(PendingDeletion, pd_id) is None
    finally:
        db.close()


def test_missing_provider_resource_treated_as_success(recovery_db, fake_local_adapter):
    """When adapter.cleanup() raises StorageNotFoundError, treat as successful cleanup."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    fake_local_adapter.cleanup_exc = StorageNotFoundError("File absent", provider="LOCAL")

    pd_id = _seed_pending(Session, quarantine_id="already-gone")

    count = service.process_pending_deletions()
    assert count == 1  # count incremented as success

    db = Session()
    try:
        # Record should be removed
        assert db.get(PendingDeletion, pd_id) is None
    finally:
        db.close()


def test_expired_lease_reclaimed(recovery_db, fake_local_adapter):
    """A record stuck in PROCESSING whose lease_expires_at is in the past is re-claimed."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    past_time = datetime.now(timezone.utc) - timedelta(seconds=60)
    pd_id = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="old-dead-token",
        lease_expires_at=past_time,
        quarantine_id="q-expired",
    )

    count = service.process_pending_deletions()
    assert count == 1
    assert fake_local_adapter.cleaned_ids == ["q-expired"]

    db = Session()
    try:
        assert db.get(PendingDeletion, pd_id) is None
    finally:
        db.close()


def test_unexpired_lease_not_claimed(recovery_db, fake_local_adapter):
    """A record in PROCESSING whose lease is still active must NOT be claimed."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    future_time = datetime.now(timezone.utc) + timedelta(seconds=300)
    pd_id = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="active-worker-token",
        lease_expires_at=future_time,
        quarantine_id="q-active",
    )

    count = service.process_pending_deletions()
    assert count == 0
    assert fake_local_adapter.cleaned_ids == []

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.status == "PROCESSING"
        assert rec.claim_token == "active-worker-token"
    finally:
        db.close()


def test_transient_failure_schedules_exponential_backoff(recovery_db, fake_local_adapter):
    """Transient errors increment retry_count, reset status to PENDING and set next_retry_at."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    fake_local_adapter.cleanup_exc = StorageUnavailableError("Server down", provider="LOCAL")

    pd_id = _seed_pending(Session, quarantine_id="q-transient")

    count = service.process_pending_deletions()
    assert count == 0  # Not successful

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.status == "PENDING"
        assert rec.retry_count == 1
        assert rec.claim_token is None
        assert "Server down" in rec.last_error
        assert rec.next_retry_at is not None
        # Backoff for first retry = 30 * 2^0 = 30 seconds
        expected_delay = timedelta(seconds=30)
        actual_delay = rec.next_retry_at - rec.last_attempted_at
        assert abs((actual_delay - expected_delay).total_seconds()) < 5
    finally:
        db.close()


def test_transient_failure_respects_retry_after(recovery_db, fake_local_adapter):
    """StorageRateLimitError with retry_after_seconds uses custom delay."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    fake_local_adapter.cleanup_exc = StorageRateLimitError(retry_after_seconds=120, provider="LOCAL")

    pd_id = _seed_pending(Session, quarantine_id="q-ratelimit")

    service.process_pending_deletions()

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec.status == "PENDING"
        assert rec.retry_count == 1
        actual_delay = (rec.next_retry_at - rec.last_attempted_at).total_seconds()
        assert abs(actual_delay - 120) < 5
    finally:
        db.close()


def test_maximum_retries_marks_permanent_failure(recovery_db, fake_local_adapter):
    """Reaching MAX_RETRIES (5) marks record as FAILED permanently."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    fake_local_adapter.cleanup_exc = StorageUnavailableError("Persistent outage", provider="LOCAL")

    pd_id = _seed_pending(Session, retry_count=MAX_RETRIES - 1, quarantine_id="q-maxretries")

    service.process_pending_deletions()

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.status == "FAILED"
        assert rec.retry_count == MAX_RETRIES
        assert rec.next_retry_at is None
        assert rec.claim_token is None
    finally:
        db.close()


def test_permanent_authentication_failure(recovery_db, fake_local_adapter):
    """StorageAuthenticationError immediately marks status as FAILED without retries."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    fake_local_adapter.cleanup_exc = StorageAuthenticationError("Token revoked", provider="LOCAL")

    pd_id = _seed_pending(Session, quarantine_id="q-authfail")

    service.process_pending_deletions()

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec.status == "FAILED"
        assert rec.retry_count == 0
        assert rec.next_retry_at is None
        assert "Token revoked" in rec.last_error
    finally:
        db.close()


def test_permanent_permission_failure(recovery_db, fake_local_adapter):
    """StoragePermissionError immediately marks status as FAILED without retries."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    fake_local_adapter.cleanup_exc = StoragePermissionError("Access denied", provider="LOCAL")

    pd_id = _seed_pending(Session, quarantine_id="q-permfail")

    service.process_pending_deletions()

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec.status == "FAILED"
        assert rec.next_retry_at is None
    finally:
        db.close()


def test_future_next_retry_at_skipped(recovery_db, fake_local_adapter):
    """A record whose next_retry_at is in the future is skipped during scan."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    future_retry = datetime.now(timezone.utc) + timedelta(minutes=10)
    pd_id = _seed_pending(Session, next_retry_at=future_retry, quarantine_id="q-future")

    count = service.process_pending_deletions()
    assert count == 0
    assert fake_local_adapter.cleaned_ids == []

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec.status == "PENDING"
    finally:
        db.close()


def test_lost_claim_ownership_during_finalize(recovery_db, fake_local_adapter):
    """If another process changes claim_token during processing, finalization is skipped."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    pd_id = _seed_pending(Session, quarantine_id="q-stolen")

    # Patch _finalize_success to simulate token mismatch
    original_finalize = service._finalize_success

    def _tampered_finalize(db, record, claim_token):
        # Steal claim in DB before finalizing
        rec = db.get(PendingDeletion, record.id)
        rec.claim_token = "thief-token"
        db.commit()
        return original_finalize(db, record, claim_token)

    service._finalize_success = _tampered_finalize

    count = service.process_pending_deletions()
    assert count == 0  # Did not finalize

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.claim_token == "thief-token"
    finally:
        db.close()


def test_lost_claim_ownership_during_retry(recovery_db, fake_local_adapter):
    """If another process steals claim_token, _schedule_retry aborts update."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    fake_local_adapter.cleanup_exc = StorageUnavailableError("Error", provider="LOCAL")
    pd_id = _seed_pending(Session, quarantine_id="q-retry-stolen")

    original_schedule = service._schedule_retry

    def _tampered_schedule(db, record, claim_token, error_msg, transient=True, retry_after=None):
        rec = db.get(PendingDeletion, record.id)
        rec.claim_token = "other-token"
        db.commit()
        return original_schedule(db, record, claim_token, error_msg, transient, retry_after)

    service._schedule_retry = _tampered_schedule

    service.process_pending_deletions()

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec.claim_token == "other-token"
        assert rec.retry_count == 0  # Schedule retry was rejected due to claim token mismatch
    finally:
        db.close()


def test_unregistered_provider_schedules_transient_retry(recovery_db):
    """Pending record for an unregistered provider triggers transient retry schedule."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    pd_id = _seed_pending(Session, provider="LOCAL", quarantine_id="q-no-adapter")

    # Ensure LOCAL adapter is NOT registered
    try:
        remove("LOCAL")
    except Exception:
        pass

    service.process_pending_deletions()

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec.status == "PENDING"
        assert rec.retry_count == 1
        assert "unavailable" in rec.last_error.lower()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Background Worker & Lifespan Integration Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recovery_poll_loop_runs_and_cancels(recovery_db, fake_local_adapter):
    """_recovery_poll_loop processes eligible items and exits cleanly on cancellation."""
    from backend.main import _recovery_poll_loop

    Session = recovery_db["Session"]
    service = recovery_db["service"]

    pd_id = _seed_pending(Session, quarantine_id="q-background-1")

    task = asyncio.create_task(_recovery_poll_loop(service))

    # Give loop a brief moment to run first iteration
    await asyncio.sleep(0.1)

    # Cancel task
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert fake_local_adapter.cleaned_ids == ["q-background-1"]
    db = Session()
    try:
        assert db.get(PendingDeletion, pd_id) is None
    finally:
        db.close()
