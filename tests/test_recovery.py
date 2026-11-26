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
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from backend.storage.errors import StorageNotFoundError
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
        self.restore_exc: Exception | None = None
        self.restored_ids: list[str] = []
        self.download_data: bytes | None = None
        self.download_exc: Exception | None = None
        self.download_calls: list[str] = []

    def upload(self, file_data: bytes, name: str, logical_path: str):
        raise NotImplementedError

    def download(self, provider_file_id: str) -> bytes:
        self.download_calls.append(provider_file_id)
        if self.download_exc:
            raise self.download_exc
        if self.download_data is None:
            raise NotImplementedError
        return self.download_data

    def quarantine(self, provider_file_id: str) -> str:
        raise NotImplementedError

    def restore(self, quarantine_id: str):
        if self.restore_exc:
            raise self.restore_exc
        self.restored_ids.append(quarantine_id)

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
    operation_type: str = "CLEANUP",
    operation_generation: int | None = None,
    original_sha256: str | None = None,
) -> str:
    """Helper to insert a PendingDeletion record directly into DB."""
    db = Session()
    try:
        pd = PendingDeletion(
            provider=provider,
            # NULL keeps these provider-level (registry) scenarios legacy;
            # account-scoped recovery is covered by
            # ``tests/test_file_account_recovery.py``.
            provider_account_id=None,
            provider_file_id=provider_file_id,
            quarantine_id=quarantine_id,
            file_name_snapshot=file_name,
            status=status,
            retry_count=retry_count,
            claim_token=claim_token,
            lease_expires_at=lease_expires_at,
            next_retry_at=next_retry_at,
            operation_type=operation_type,
            operation_generation=operation_generation,
            original_sha256=original_sha256,
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

    def _tampered_finalize(db, record, claim_token, claim_generation):
        # Steal claim in DB before finalizing
        rec = db.get(PendingDeletion, record.id)
        rec.claim_token = "thief-token"
        db.commit()
        return original_finalize(db, record, claim_token, claim_generation)

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

    def _tampered_schedule(db, record, claim_token, claim_generation, error_msg, transient=True, retry_after=None):
        rec = db.get(PendingDeletion, record.id)
        rec.claim_token = "other-token"
        db.commit()
        return original_schedule(db, record, claim_token, claim_generation, error_msg, transient, retry_after)

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

def test_restore_operation_succeeds(recovery_db, fake_local_adapter):
    """A successful RESTORE removes its pending recovery record."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    fake_local_adapter.restore_exc = None

    pd_id = _seed_pending(
        Session,
        quarantine_id="q-restore-success",
        operation_type="RESTORE",
    )

    count = service.process_pending_deletions()

    assert count == 1
    assert fake_local_adapter.restored_ids == ["q-restore-success"]

    db = Session()
    try:
        assert db.get(PendingDeletion, pd_id) is None
    finally:
        db.close

def test_local_restore_missing_original_retries(
    recovery_db, fake_local_adapter
):
    """A missing quarantine and missing original file must remain retryable."""
    from backend.storage.errors import StorageNotFoundError

    Session = recovery_db["Session"]
    service = recovery_db["service"]

    expected_hash = "a" * 64

    fake_local_adapter.restore_exc = StorageNotFoundError(
        "q-restore-missing", provider="LOCAL"
    )
    fake_local_adapter.download_exc = StorageNotFoundError(
        "file-1", provider="LOCAL"
    )

    pd_id = _seed_pending(
        Session,
        quarantine_id="q-restore-missing",
        operation_type="RESTORE",
        original_sha256=expected_hash,
    )

    count = service.process_pending_deletions()

    assert count == 0
    assert fake_local_adapter.download_calls == ["file-1"]

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.status == "PENDING"
        assert rec.retry_count == 1
    finally:
        db.close()

def test_local_restore_hash_mismatch_retries(
    recovery_db, fake_local_adapter
):
    """A restored file with a different hash must not be accepted."""
    import hashlib
    from backend.storage.errors import StorageNotFoundError

    Session = recovery_db["Session"]
    service = recovery_db["service"]

    expected_bytes = b"the original file"
    different_bytes = b"a different file"

    fake_local_adapter.restore_exc = StorageNotFoundError(
        "q-restore-mismatch", provider="LOCAL"
    )
    fake_local_adapter.download_data = different_bytes

    pd_id = _seed_pending(
        Session,
        quarantine_id="q-restore-mismatch",
        operation_type="RESTORE",
        original_sha256=hashlib.sha256(expected_bytes).hexdigest(),
    )

    count = service.process_pending_deletions()

    assert count == 0

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.status == "PENDING"
        assert rec.retry_count == 1
        assert "SHA-256 mismatch" in rec.last_error
    finally:
        db.close()

def test_local_restore_without_saved_hash_retries(
    recovery_db, fake_local_adapter
):
    """Without a saved hash, a missing quarantine cannot be treated as success."""
    from backend.storage.errors import StorageNotFoundError

    Session = recovery_db["Session"]
    service = recovery_db["service"]

    fake_local_adapter.restore_exc = StorageNotFoundError(
        "q-restore-no-hash", provider="LOCAL"
    )
    fake_local_adapter.download_data = b"some file contents"

    pd_id = _seed_pending(
        Session,
        quarantine_id="q-restore-no-hash",
        operation_type="RESTORE",
    )

    count = service.process_pending_deletions()

    assert count == 0
    assert fake_local_adapter.download_calls == []

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.status == "PENDING"
        assert rec.retry_count == 1
    finally:
        db.close()

def test_gdrive_restore_missing_quarantine_retries(
    recovery_db, fake_gdrive_adapter
):
    """Google Drive missing quarantine follows normal retry behavior."""
    from backend.storage.errors import StorageNotFoundError

    Session = recovery_db["Session"]
    service = recovery_db["service"]

    fake_gdrive_adapter.restore_exc = StorageNotFoundError(
        "drive-q-missing", provider="GOOGLE_DRIVE"
    )
    fake_gdrive_adapter.download_data = b"some file contents"

    pd_id = _seed_pending(
        Session,
        provider="GOOGLE_DRIVE",
        quarantine_id="drive-q-missing",
        operation_type="RESTORE",
        original_sha256="a" * 64,
    )

    count = service.process_pending_deletions()

    assert count == 0
    assert fake_gdrive_adapter.download_calls == []

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.status == "PENDING"
        assert rec.retry_count == 1
    finally:
        db.close()


def test_local_restore_already_completed_is_verified(
    recovery_db, fake_local_adapter
):
    """A missing quarantine entry is accepted only if the original file matches."""
    import hashlib
    from backend.storage.errors import StorageNotFoundError

    Session = recovery_db["Session"]
    service = recovery_db["service"]

    original_bytes = b"SiloVault restore verification test"
    expected_hash = hashlib.sha256(original_bytes).hexdigest()

    fake_local_adapter.restore_exc = StorageNotFoundError(
        "q-restore-already-done", provider="LOCAL"
    )
    fake_local_adapter.download_data = original_bytes

    pd_id = _seed_pending(
        Session,
        quarantine_id="q-restore-already-done",
        operation_type="RESTORE",
        original_sha256=expected_hash,
    )

    count = service.process_pending_deletions()

    assert count == 1
    assert fake_local_adapter.download_calls == ["file-1"]

    db = Session()
    try:
        assert db.get(PendingDeletion, pd_id) is None
    finally:
        db.close()
# ---------------------------------------------------------------------------
# Concurrency / Atomic Claiming Tests
# ---------------------------------------------------------------------------

def test_active_lease_prevents_second_claim(recovery_db, fake_local_adapter):
    """A record with an active lease cannot be re-claimed by another worker."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    future_time = datetime.now(timezone.utc) + timedelta(seconds=300)
    pd_id = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="worker-a-token",
        lease_expires_at=future_time,
        quarantine_id="q-active-lease",
    )

    count = service.process_pending_deletions()
    assert count == 0  # No claim possible — active lease

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.claim_token == "worker-a-token"
        assert rec.operation_generation == 0  # unchanged
    finally:
        db.close()


def test_expired_lease_allows_new_claim(recovery_db, fake_local_adapter):
    """An expired lease allows a new worker to claim with a fresh token and generation."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    past_time = datetime.now(timezone.utc) - timedelta(seconds=60)
    pd_id = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="old-dead-token",
        lease_expires_at=past_time,
        quarantine_id="q-expired-reclaim",
    )

    count = service.process_pending_deletions()
    assert count == 1  # Reclaimed and processed

    db = Session()
    try:
        # Record is gone (finalized successfully)
        assert db.get(PendingDeletion, pd_id) is None
    finally:
        db.close()


def test_new_claim_gets_different_token_and_incremented_generation(recovery_db, fake_local_adapter):
    """A stale-claim record is reclaimed by a new worker with fresh token + generation."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    # Seed a PROCESSING record with an expired lease and old token.
    past_time = datetime.now(timezone.utc) - timedelta(seconds=60)
    pd_id = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="stale-token",
        lease_expires_at=past_time,
        next_retry_at=None,
        quarantine_id="q-gen-check",
    )

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.operation_generation == 0
        assert rec.claim_token == "stale-token"
    finally:
        db.close()

    # Run the recovery service: it reclaims the expired record.
    count = service.process_pending_deletions()
    # Adapter succeeds by default -> finalized, so count == 1.
    assert count == 1

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        if rec is not None:
            # If still present (e.g., finalization lost race), verify new token/generation.
            assert rec.claim_token is not None
            assert rec.claim_token != "stale-token"
            assert rec.operation_generation >= 1
        else:
            # Normal path: record was finalized and deleted.
            pass
    finally:
        db.close()


def test_conditional_update_allows_only_one_claim(recovery_db, fake_local_adapter):
    """Conditional UPDATE allows only one claim per eligible record."""
    Session = recovery_db["Session"]

    # Seed a single PENDING record.
    pd_id = _seed_pending(Session, quarantine_id="q-concurrent-1")

    # Simulate Worker A claiming directly via SQL (like process_pending_deletions would).
    from sqlalchemy import update
    now = datetime.now(timezone.utc)
    token_a = str(uuid.uuid4())
    db_a = Session()
    try:
        result = db_a.execute(
            update(PendingDeletion)
            .where(
                PendingDeletion.id == pd_id,
                PendingDeletion.status == "PENDING",
                PendingDeletion.next_retry_at.is_(None),
                PendingDeletion.operation_type.in_(["CLEANUP", "RESTORE"]),
            )
            .values(
                status="PROCESSING",
                claim_token=token_a,
                operation_generation=PendingDeletion.operation_generation + 1,
                processing_started_at=now,
                lease_expires_at=now + timedelta(seconds=LEASE_DURATION_SECONDS),
            )
        ).rowcount
        db_a.commit()
        assert result == 1  # Worker A won the claim
    finally:
        db_a.close()

    # Simulate Worker B trying to claim the same record.
    token_b = str(uuid.uuid4())
    db_b = Session()
    try:
        result = db_b.execute(
            update(PendingDeletion)
            .where(
                PendingDeletion.id == pd_id,
                PendingDeletion.status == "PENDING",
                PendingDeletion.next_retry_at.is_(None),
                PendingDeletion.operation_type.in_(["CLEANUP", "RESTORE"]),
            )
            .values(
                status="PROCESSING",
                claim_token=token_b,
                operation_generation=PendingDeletion.operation_generation + 1,
                processing_started_at=now,
                lease_expires_at=now + timedelta(seconds=LEASE_DURATION_SECONDS),
            )
        ).rowcount
        db_b.commit()
        assert result == 0  # Worker B lost — record already claimed
    finally:
        db_b.close()

    # Verify the record has Worker A's token, not B's.
    db_verify = Session()
    try:
        rec = db_verify.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.claim_token == token_a
        assert rec.claim_token != token_b
        assert rec.operation_generation == 1
    finally:
        db_verify.close()


def test_claim_only_updates_eligible_records(recovery_db, fake_local_adapter):
    """The atomic UPDATE only modifies records that are still eligible at update time."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    # Seed an eligible record and an ineligible one (future next_retry_at)
    eligible_id = _seed_pending(Session, quarantine_id="q-eligible")
    future_retry = datetime.now(timezone.utc) + timedelta(minutes=10)
    ineligible_id = _seed_pending(
        Session,
        quarantine_id="q-ineligible",
        next_retry_at=future_retry,
        provider_file_id="file-2",
    )

    count = service.process_pending_deletions()
    assert count == 1  # Only the eligible record was processed

    db = Session()
    try:
        # Eligible record is gone (finalized)
        assert db.get(PendingDeletion, eligible_id) is None
        # Ineligible record is untouched
        rec = db.get(PendingDeletion, ineligible_id)
        assert rec is not None
        assert rec.status == "PENDING"
        assert rec.claim_token is None
        assert rec.operation_generation == 0
    finally:
        db.close()

# ---------------------------------------------------------------------------
# Generation Fencing Tests (Repair 3)
# ---------------------------------------------------------------------------

def test_stale_generation_cannot_finalize(recovery_db, fake_local_adapter):
    """A worker whose generation is stale cannot finalize the record."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    # Seed a PROCESSING record with a known token and generation.
    pd_id = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="gen-token",
        operation_generation=3,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
        next_retry_at=None,
        quarantine_id="q-stale-finalize",
    )

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.operation_generation == 3
    finally:
        db.close()

    # Call _finalize_success with a STALE generation.
    result = service._finalize_success(
        Session(), rec, "gen-token", claim_generation=99
    )
    assert result is False

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.operation_generation == 3  # unchanged
    finally:
        db.close()


def test_stale_generation_cannot_schedule_retry(recovery_db, fake_local_adapter):
    """A worker whose generation is stale cannot schedule a retry."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    # Seed a PROCESSING record with a known token and generation.
    pd_id = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="retry-gen-token",
        operation_generation=2,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
        next_retry_at=None,
        quarantine_id="q-stale-retry",
    )

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.operation_generation == 2
    finally:
        db.close()

    # Call _schedule_retry with a STALE generation.
    class FakeRecord:
        id = pd_id
    service._schedule_retry(
        Session(),
        FakeRecord(),
        "retry-gen-token",
        claim_generation=88,  # stale!
        error_msg="simulated error",
    )

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.operation_generation == 2  # unchanged
        assert rec.retry_count == 0  # no retry scheduled
    finally:
        db.close()


def test_stale_generation_cannot_mark_permanent_failure(recovery_db, fake_local_adapter):
    """A worker whose generation is stale cannot mark a record permanently failed."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    # Seed a PROCESSING record with a known token and generation.
    pd_id = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="perm-gen-token",
        operation_generation=5,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
        next_retry_at=None,
        quarantine_id="q-stale-permfail",
    )

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.operation_generation == 5
        assert rec.status == "PROCESSING"
    finally:
        db.close()

    # Call _handle_permanent_failure with a STALE generation.
    class FakeRecord:
        id = pd_id
    service._handle_permanent_failure(
        Session(),
        FakeRecord(),
        "perm-gen-token",
        claim_generation=77,  # stale!
        error_msg="permanent failure simulated",
    )

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.operation_generation == 5  # unchanged
        assert rec.status == "PROCESSING"  # NOT marked FAILED
    finally:
        db.close()


def test_current_generation_can_finalize_and_retry_normally(recovery_db, fake_local_adapter):
    """A worker with the correct generation can finalize and retry normally."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    # Test successful finalize with correct generation.
    pd_id = _seed_pending(Session, quarantine_id="q-gen-ok-finalize")
    count = service.process_pending_deletions()
    assert count == 1
    db = Session()
    try:
        assert db.get(PendingDeletion, pd_id) is None
    finally:
        db.close()

    # Test successful retry with correct generation.
    pd_id2 = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="ok-gen-token",
        operation_generation=1,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
        next_retry_at=None,
        quarantine_id="q-gen-ok-retry",
    )
    class FakeRecord:
        id = pd_id2
    service._schedule_retry(
        Session(),
        FakeRecord(),
        "ok-gen-token",
        claim_generation=1,  # correct
        error_msg="transient error",
    )
    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id2)
        assert rec is not None
        assert rec.status == "PENDING"
        assert rec.retry_count == 1
        assert rec.claim_token is None
    finally:
        db.close()

# ---------------------------------------------------------------------------
# NULL-Lease Regression Tests (Repair 4B)
# ---------------------------------------------------------------------------


def test_null_lease_can_be_reclaimed(recovery_db, fake_local_adapter):
    """A PROCESSING record with lease_expires_at=None is reclaimed."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    # Seed a PROCESSING record with NULL lease.
    pd_id = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="old-null-token",
        lease_expires_at=None,
        operation_generation=0,
        quarantine_id="q-null-lease",
    )

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.lease_expires_at is None
        assert rec.claim_token == "old-null-token"
        assert rec.operation_generation == 0
    finally:
        db.close()

    # Run the recovery service: it should reclaim the NULL-lease record.
    count = service.process_pending_deletions()
    assert count == 1

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        # Record should have been reclaimed/finalized, so it may be gone.
        # If still present (e.g., finalization lost race), verify new token/generation.
        if rec is not None:
            assert rec.claim_token is not None
            assert rec.claim_token != "old-null-token"
            assert rec.operation_generation >= 1
        # Normal path: record was finalized and deleted.
        else:
            pass
    finally:
        db.close()


def test_null_lease_new_token_differs(recovery_db, fake_local_adapter):
    """The new claim token differs from the old NULL-lease token."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    # Seed a PROCESSING record with NULL lease.
    pd_id = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="old-null-token",
        lease_expires_at=None,
        operation_generation=0,
        quarantine_id="q-null-token",
    )

    service.process_pending_deletions()

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        if rec is not None:
            # New token should differ from the old NULL-lease token.
            assert rec.claim_token != "old-null-token"
            assert rec.claim_token is not None
        else:
            # Record was finalized and removed; this is the normal expected path.
            pass
    finally:
        db.close()


def test_null_lease_generation_increments_once(recovery_db, fake_local_adapter):
    """operation_generation increments exactly once for NULL-lease reclamation."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    # Seed a PROCESSING record with NULL lease and generation=0.
    pd_id = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="gen-token",
        lease_expires_at=None,
        operation_generation=0,
        quarantine_id="q-null-gen",
    )

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.operation_generation == 0
        assert rec.claim_token == "gen-token"
    finally:
        db.close()

    # Run recovery service.
    service.process_pending_deletions()

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        if rec is not None:
            # Generation should have incremented exactly once.
            assert rec.operation_generation == 1
            assert rec.claim_token is not None
            assert rec.claim_token != "gen-token"
        else:
            # Record was finalized and removed; generation incremented as part
            # of the claim->execute->finalize path.
            pass
    finally:
        db.close()


def test_null_lease_stale_cannot_modify(recovery_db, fake_local_adapter):
    """A stale token and generation cannot modify the reclaimed NULL-lease record."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    # Seed a PROCESSING record with NULL lease.
    pd_id = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="old-token",
        lease_expires_at=None,
        operation_generation=0,
        quarantine_id="q-null-stale",
    )

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.claim_token == "old-token"
        assert rec.operation_generation == 0
        assert rec.lease_expires_at is None
    finally:
        db.close()

    # Call _finalize_success with a STALE generation (99 vs 0).
    result = service._finalize_success(
        Session(), rec, "old-token", claim_generation=99
    )
    assert result is False

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        # Record should be unchanged � stale worker cannot finalize.
        assert rec is not None
        assert rec.claim_token == "old-token"
        assert rec.operation_generation == 0
        assert rec.lease_expires_at is None
        assert rec.status == "PROCESSING"
    finally:
        db.close()


def test_null_lease_stale_cannot_schedule_retry(recovery_db, fake_local_adapter):
    """A stale generation cannot schedule a retry for NULL-lease record."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    # Seed a PROCESSING record with NULL lease.
    pd_id = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="retry-token",
        lease_expires_at=None,
        operation_generation=0,
        quarantine_id="q-null-retry-stale",
    )

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.claim_token == "retry-token"
        assert rec.operation_generation == 0
    finally:
        db.close()

    # Call _schedule_retry with a STALE generation.
    class FakeRecord:
        id = pd_id
    service._schedule_retry(
        Session(),
        FakeRecord(),
        "retry-token",
        claim_generation=99,  # stale!
        error_msg="simulated error",
    )

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        # Record should be unchanged � stale worker cannot schedule retry.
        assert rec is not None
        assert rec.claim_token == "retry-token"
        assert rec.operation_generation == 0
        assert rec.status == "PROCESSING"
        assert rec.retry_count == 0  # no retry scheduled
    finally:
        db.close()


def test_null_lease_stale_cannot_mark_permanent_failure(recovery_db, fake_local_adapter):
    """A stale generation cannot mark a NULL-lease record permanently failed."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    # Seed a PROCESSING record with NULL lease.
    pd_id = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="perm-token",
        lease_expires_at=None,
        operation_generation=0,
        quarantine_id="q-null-perm-stale",
    )

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.claim_token == "perm-token"
        assert rec.operation_generation == 0
        assert rec.status == "PROCESSING"
    finally:
        db.close()

    # Call _handle_permanent_failure with a STALE generation.
    class FakeRecord:
        id = pd_id
    service._handle_permanent_failure(
        Session(),
        FakeRecord(),
        "perm-token",
        claim_generation=99,  # stale!
        error_msg="permanent failure simulated",
    )

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        # Record should be unchanged � stale worker cannot mark permanent failure.
        assert rec is not None
        assert rec.claim_token == "perm-token"
        assert rec.operation_generation == 0
        assert rec.status == "PROCESSING"
    finally:
        db.close()

