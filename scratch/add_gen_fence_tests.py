"""Append generation fencing tests to test_recovery.py."""

path = r'c:\Projects\SiloVault\tests\test_recovery.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

new_tests = '''

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
'''

if new_tests not in content:
    content = content.rstrip() + new_tests
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    print("Generation fencing tests appended successfully")
else:
    print("Tests already present")
