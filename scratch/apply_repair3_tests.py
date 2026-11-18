"""Apply Repair 3 test updates for generation fencing."""

path = r'c:\Projects\SiloVault\tests\test_recovery.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# Fix test_lost_claim_ownership_during_finalize: add claim_generation param to mock
old = '''    def _tampered_finalize(db, record, claim_token):
        # Steal claim in DB before finalizing
        rec = db.get(PendingDeletion, record.id)
        rec.claim_token = "thief-token"
        db.commit()
        return original_finalize(db, record, claim_token)'''
new = '''    def _tampered_finalize(db, record, claim_token, claim_generation):
        # Steal claim in DB before finalizing
        rec = db.get(PendingDeletion, record.id)
        rec.claim_token = "thief-token"
        db.commit()
        return original_finalize(db, record, claim_token, claim_generation)'''
if old not in content:
    print("ERROR: finalize mock signature not found")
    exit(1)
content = content.replace(old, new, 1)

# Fix test_lost_claim_ownership_during_retry: add claim_generation param to mock
old = '''    def _tampered_schedule(db, record, claim_token, error_msg, transient=True, retry_after=None):
        rec = db.get(PendingDeletion, record.id)
        rec.claim_token = "other-token"
        db.commit()
        return original_schedule(db, record, claim_token, error_msg, transient, retry_after)'''
new = '''    def _tampered_schedule(db, record, claim_token, claim_generation, error_msg, transient=True, retry_after=None):
        rec = db.get(PendingDeletion, record.id)
        rec.claim_token = "other-token"
        db.commit()
        return original_schedule(db, record, claim_token, claim_generation, error_msg, transient, retry_after)'''
if old not in content:
    print("ERROR: retry mock signature not found")
    exit(1)
content = content.replace(old, new, 1)

# Append new generation fencing tests
new_tests = '''

def test_stale_generation_cannot_finalize(recovery_db, fake_local_adapter):
    """A worker whose generation is stale cannot finalize the record."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    pd_id = _seed_pending(Session, quarantine_id="q-stale-finalize")

    # Claim the record via the service (generates token + generation).
    count = service.process_pending_deletions()
    assert count == 1  # Successful cleanup

    # Now seed a fresh PENDING record and manually set an old generation.
    old_gen = 0
    pd_id2 = _seed_pending(Session, quarantine_id="q-stale-finalize-2")
    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id2)
        # Advance generation without changing token — simulate a stolen claim.
        rec.operation_generation = 999
        db.commit()
    finally:
        db.close()

    # Process again: the expired/old-generation path won't match because
    # generation fencing rejects it. Record stays PENDING with generation=999.
    count = service.process_pending_deletions()
    # The record is PENDING but has generation=999, and the query requires
    # lease_expires_at < now for PROCESSING; this one is PENDING so it should be claimed.
    # But the finalization will fail due to generation mismatch.
    # Actually: since it's PENDING, the atomic UPDATE will claim it with a NEW generation.
    # We want to test that a STALE generation blocks finalization AFTER claim.
    # So instead, we seed it as PROCESSING with matching token but wrong generation.
    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id2)
        if rec is None:
            # It was consumed by the last process_pending_deletions call.
            # Seed again.
            rec = PendingDeletion(
                provider="LOCAL",
                provider_account_id=None,
                provider_file_id="file-stale-finalize",
                quarantine_id="q-stale-finalize-2b",
                file_name_snapshot="test.txt",
                status="PROCESSING",
                claim_token="some-token",
                operation_generation=5,  # stale generation
                lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
                operation_type="CLEANUP",
            )
            db.add(rec)
            db.commit()
            db.refresh(rec)
            pd_id2 = rec.id
        else:
            rec.status = "PROCESSING"
            rec.claim_token = "some-token"
            rec.operation_generation = 5  # stale vs whatever the real claim would get
            rec.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=300)
            db.commit()
            pd_id2 = rec.id
    finally:
        db.close()

    # Reset adapter to succeed, then run the service.
    fake_local_adapter.cleanup_exc = None
    count = service.process_pending_deletions()
    # The service will re-fetch the record and see claim_token != its own -> skip.
    # We need to verify the generation fence works inside _finalize_success itself.
    # Instead, directly call _finalize_success with a stale generation.
    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id2)
        assert rec is not None
        assert rec.operation_generation == 5
        result = service._finalize_success(db, rec, "wrong-token", claim_generation=42)
        assert result is False
        # Record unchanged.
        rec = db.get(PendingDeletion, pd_id2)
        assert rec.operation_generation == 5
    finally:
        db.close()


def test_stale_generation_cannot_schedule_retry(recovery_db, fake_local_adapter):
    """A worker whose generation is stale cannot schedule a retry."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    pd_id = _seed_pending(Session, quarantine_id="q-stale-retry")

    # Claim the record first so it gets a generation.
    count = service.process_pending_deletions()
    assert count == 1  # finalized

    # Seed a PROCESSING record with a known token/generation.
    pd_id2 = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="gen-token",
        operation_generation=3,
        next_retry_at=None,
        quarantine_id="q-stale-retry-2",
    )

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id2)
        assert rec is not None
        assert rec.operation_generation == 3
    finally:
        db.close()

    # Call _schedule_retry with a STALE generation (different from DB value).
    service._schedule_retry(
        Session(),
        type('R', (), {"id": pd_id2})(),
        "gen-token",
        claim_generation=99,  # stale!
        error_msg="simulated error",
    )
    # Use the same session used above; refresh it.
    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id2)
        assert rec is not None
        assert rec.operation_generation == 3  # unchanged
        assert rec.retry_count == 0  # no retry scheduled
    finally:
        db.close()


def test_stale_generation_cannot_mark_permanent_failure(recovery_db, fake_local_adapter):
    """A worker whose generation is stale cannot mark a record permanently failed."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    pd_id = _seed_pending(Session, quarantine_id="q-stale-permfail")

    # Seed a PROCESSING record with a known token/generation.
    pd_id2 = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="perm-token",
        operation_generation=7,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
        next_retry_at=None,
        quarantine_id="q-stale-permfail-2",
    )

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id2)
        assert rec is not None
        assert rec.operation_generation == 7
        assert rec.status == "PROCESSING"
    finally:
        db.close()

    # Call _handle_permanent_failure with a STALE generation.
    service._handle_permanent_failure(
        Session(),
        type('R', (), {"id": pd_id2})(),
        "perm-token",
        claim_generation=88,  # stale!
        error_msg="permanent failure simulated",
    )

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id2)
        assert rec is not None
        assert rec.operation_generation == 7  # unchanged
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
        claim_token="ok-token",
        operation_generation=1,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
        next_retry_at=None,
        quarantine_id="q-gen-ok-retry",
    )
    service._schedule_retry(
        Session(),
        type('R', (), {"id": pd_id2})(),
        "ok-token",
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


def test_existing_lost_token_tests_still_pass(recovery_db, fake_local_adapter):
    """Verify that the existing lost-token tests continue working after generation fencing."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    # Finalize loss test.
    pd_id = _seed_pending(Session, quarantine_id="q-loss-test-finalize")
    original_finalize = service._finalize_success
    def tampered_finalize(db, record, claim_token, claim_generation):
        rec = db.get(PendingDeletion, record.id)
        rec.claim_token = "thief-token"
        db.commit()
        return original_finalize(db, record, claim_token, claim_generation)
    service._finalize_success = tampered_finalize

    count = service.process_pending_deletions()
    assert count == 0
    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.claim_token == "thief-token"
    finally:
        db.close()

    # Retry loss test.
    fake_local_adapter.cleanup_exc = StorageUnavailableError("Error", provider="LOCAL")
    pd_id2 = _seed_pending(Session, quarantine_id="q-loss-test-retry")
    original_schedule = service._schedule_retry
    def tampered_schedule(db, record, claim_token, claim_generation, error_msg, transient=True, retry_after=None):
        rec = db.get(PendingDeletion, record.id)
        rec.claim_token = "other-token"
        db.commit()
        return original_schedule(db, record, claim_token, claim_generation, error_msg, transient, retry_after)
    service._schedule_retry = tampered_schedule

    service.process_pending_deletions()
    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id2)
        assert rec.claim_token == "other-token"
        assert rec.retry_count == 0
    finally:
        db.close()
'''

if new_tests not in content:
    content = content.rstrip() + new_tests
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    print("New tests appended successfully")
else:
    print("Tests already present")

print("Test script applied")
