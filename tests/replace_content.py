with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r") as f:
    c = f.read()

old_test_func = """def test_two_sessions_compete_exactly_one_claim(recovery_db, fake_local_adapter):
    \"\"\"Two separate sessions claiming the same record: exactly one wins.\"\"\"\n    Session = recovery_db["Session"]\n    service = recovery_db["service"]\n\n    pd_id = _seed_pending(Session, quarantine_id="q-race-1")\n\n    # Simulate Worker A claiming first (via the service)\n    count_a = service.process_pending_deletions()\n    assert count_a == 1  # Worker A processed and finalized the record\n\n    # Worker B runs after Worker A has already deleted the record.\n    # The record is gone, so no claim is possible.\n    count_b = service.process_pending_deletions()\n    assert count_b == 0\n\n    db = Session()\n    try:\n        assert db.get(PendingDeletion, pd_id) is None\n    finally:\n        db.close()"""

new_test_func = """def test_atomic_claim_guarantees_single_session(recovery_db, fake_local_adapter):
    \"\"\"Two independent sessions compete for the same eligible record.

    Exactly one session obtains the claim via the conditional UPDATE that
    matches the production eligibility conditions (status, lease, next_retry_at,
    operation_type). The second session observes rowcount == 0 and the
    database retains the first session's claim token, PROCESSING status, and
    operation_generation == 1.
    \"\"\"\n    from sqlalchemy import update\n    import uuid\n    from datetime import datetime, timedelta\n    from backend.services.recovery import LEASE_DURATION_SECONDS\n\n    Session = recovery_db["Session"]\n    service = recovery_db["service"]\n\n    # Seed a single PENDING record.\n    pd_id = _seed_pending(Session, quarantine_id="q-atomic-1")\n\n    # --- Session A: conditional claim UPDATE ---\n    now = datetime.now(timezone.utc)\n    token_a = str(uuid.uuid4())\n    db_a = Session()\n    try:\n        result = db_a.execute(
            update(PendingDeletion)
            .where(
                PendingDeletion.id == pd_id,
                PendingDeletion.status == "PENDING",
                PendingDeletion.next_retry_at.is_(None),
                PendingDeletion.operation_type.in_([\"CLEANUP\", \"RESTORE\"]),
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
        assert result == 1, f\"Session A expected 1 row, got {result}\"
    finally:
        db_a.close()

    # Commit Session A so the claim is visible to Session B.\n    db_a_verify = Session()\n    try:\n        rec = db_a_verify.get(PendingDeletion, pd_id)\n        assert rec is not None\n        assert rec.status == "PROCESSING\"\n        assert rec.claim_token == token_a\n        assert rec.operation_generation == 1\n    finally:\n        db_a_verify.close()\n\n    # --- Session B: attempt the same conditional claim ---\n    db_b = Session()\n    try:\n        result = db_b.execute(
            update(PendingDeletion)
            .where(
                PendingDeletion.id == pd_id,
                PendingDeletion.status == "PENDING",
                PendingDeletion.next_retry_at.is_(None),
                PendingDeletion.operation_type.in_([\"CLEANUP\", \"RESTORE\"]),
            )
            .values(
                status="PROCESSING",
                claim_token=str(uuid.uuid4()),
                operation_generation=PendingDeletion.operation_generation + 1,
                processing_started_at=now,
                lease_expires_at=now + timedelta(seconds=LEASE_DURATION_SECONDS),
            )
        ).rowcount\n        db_b.commit()\n        assert result == 0, f\"Session B expected 0 rows, got {result}\"
    finally:\n        db_b.close()\n\n    # --- Verify the database state ---\n    db_verify = Session()\n    try:\n        rec = db_verify.get(PendingDeletion, pd_id)\n        assert rec is not None\n        # Session A's claim should be visible;\n        # Session B's claim was rejected because the row no longer matches\n        # the WHERE conditions (status is no longer PENDING).\n        assert rec.claim_token == token_a\n        assert rec.status == \"PROCESSING\"\n        assert rec.operation_generation == 1\n    finally:\n        db_verify.close()"""

if old_test_func in c:
    c = c.replace(old_test_func, new_test_func)
    print("Replacement successful")
else:
    print("Old test not found with exact match")
    idx = c.find("def test_two_sessions_compete_exactly_one_claim")
    print(f"Found at: {idx}")

with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "w") as f:
    f.write(c)
print("File written")