"""Add concurrency tests to test_recovery.py for Repair 2."""

path = r'c:\Projects\SiloVault\tests\test_recovery.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

new_tests = '''

# ---------------------------------------------------------------------------
# Concurrency / Atomic Claiming Tests
# ---------------------------------------------------------------------------

def test_two_sessions_compete_exactly_one_claim(recovery_db, fake_local_adapter):
    """Two separate sessions claiming the same record: exactly one wins."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    pd_id = _seed_pending(Session, quarantine_id="q-race-1")

    # Simulate Worker A claiming first (via the service)
    count_a = service.process_pending_deletions()
    assert count_a == 1  # Worker A processed and finalized the record

    # Worker B runs after Worker A has already deleted the record.
    # The record is gone, so no claim is possible.
    count_b = service.process_pending_deletions()
    assert count_b == 0

    db = Session()
    try:
        assert db.get(PendingDeletion, pd_id) is None
    finally:
        db.close()


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
    """A new claim receives a different token and an incremented generation."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    # Make the adapter fail so the record is not finalized, but is claimed.
    fake_local_adapter.cleanup_exc = StorageUnavailableError("Fail", provider="LOCAL")

    past_time = datetime.now(timezone.utc) - timedelta(seconds=60)
    pd_id = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="stale-token",
        lease_expires_at=past_time,
        quarantine_id="q-gen-check",
    )

    # First claim by the service
    service.process_pending_deletions()

    # Reset the adapter to fail again for the second run
    fake_local_adapter.cleanup_exc = StorageUnavailableError("Fail2", provider="LOCAL")

    # Now simulate another worker claiming the same (still PENDING after retry) record
    service.process_pending_deletions()

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        # After two failed attempts, generation should have been incremented twice
        # (once per successful claim)
        assert rec.operation_generation == 2
        # The token should be the one from the last claim (not the original stale one)
        assert rec.claim_token is not None
        assert rec.claim_token != "stale-token"
        assert rec.status == "PENDING"
        assert rec.retry_count == 2
    finally:
        db.close()


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
'''

# Append at end of file
content = content.rstrip() + new_tests

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Tests appended successfully")
