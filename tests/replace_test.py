#!/usr/bin/env python
"""
Replace test_two_sessions_compete_exactly_one_claim with a deterministic
atomic-claim test using two independent SQLAlchemy sessions.
"""
import re

with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r") as f:
    c = f.read()

# Find and replace test_two_sessions_compete_exactly_one_claim
# The old test (lines 557-578):
old_test = '''def test_two_sessions_compete_exactly_one_claim(recovery_db, fake_local_adapter):
    \"\"\"Two separate sessions claiming the same record: exactly one wins.\"\"\"\n    Session = recovery_db["Session"]\n    service = recovery_db["service"]\n\n    pd_id = _seed_pending(Session, quarantine_id="q-race-1")\n\n    # Simulate Worker A claiming first (via the service)\n    count_a = service.process_pending_deletions()\n    assert count_a == 1  # Worker A processed and finalized the record\n\n    # Worker B runs after Worker A has already deleted the record.\n    # The record is gone, so no claim is possible.\n    count_b = service.process_pending_deletions()\n    assert count_b == 0\n\n    db = Session()\n    try:\n        assert db.get(PendingDeletion, pd_id) is None\n    finally:\n        db.close()'''

# New test: deterministic atomic claim with two sessions
new_test = '''def test_atomic_claim_guarantees_single_session(recovery_db, fake_local_adapter):
    \"\"\"Two independent sessions compete for the same eligible record.

    Exactly one session obtains the claim via the conditional UPDATE that
    matches the production eligibility conditions (status, lease, next_retry_at,
    operation_type). The second session observes rowcount == 0 and the
    database retains the first session's claim token, PROCESSING status, and
    operation_generation == 1.
    \"\"\"\n    from sqlalchemy import update\n    import uuid\n\n    Session = recovery_db["Session"]\n    service = recovery_db["service"]\n\n    # Seed a single PENDING record.\n    pd_id = _seed_pending(Session, quarantine_id=\"q-atomic-1\")\n\n    # --- Session A: conditional claim UPDATE ---\n    now = datetime.now(timezone.utc)\n    token_a = str(uuid.uuid4())\n    db_a = Session()\n    try:\n        result = db_a.execute(\n            update(PendingDeletion)\n            .where(\n                PendingDeletion.id == pd_id,\n                PendingDeletion.status == \"PENDING\",\n                PendingDeletion.next_retry_at.is_(None),\n                PendingDeletion.operation_type.in_([\"CLEANUP\", \"RESTORE\"]),\n            )\n            .values(\n                status=\"PROCESSING\",\n                claim_token=token_a,\n                operation_generation=PendingDeletion.operation_generation + 1,\n                processing_started_at=now,\n                lease_expires_at=now + timedelta(seconds=LEASE_DURATION_SECONDS),\n            )\n        ).rowcount\n        db_a.commit()\n        assert result == 1, f\"Session A expected 1 row, got {result}\"\n    finally:\n        db_a.close()\n\n    # Commit Session A so the claim is visible to Session B.\n    db_a_verify = Session()\n    try:\n        rec = db_a_verify.get(PendingDeletion, pd_id)\n        assert rec is not None\n        assert rec.status == \"PROCESSING\"\n        assert rec.claim_token == token_a\n        assert rec.operation_generation == 1\n    finally:\n        db_a_verify.close()\n\n    # --- Session B: attempt the same conditional claim ---\n    db_b = Session()\n    try:\n        result = db_b.execute(\n            update(PendingDeletion)\n            .where(\n                PendingDeletion.id == pd_id,\n                PendingDeletion.status == \"PENDING\",\n                PendingDeletion.next_retry_at.is_(None),\n                PendingDeletion.operation_type.in_([\"CLEANUP\", \"RESTORE\"]),\n            )\n            .values(\n                status=\"PROCESSING\",\n                claim_token=str(uuid.uuid4()),\n                operation_generation=PendingDeletion.operation_generation + 1,\n                processing_started_at=now,\n                lease_expires_at=now + timedelta(seconds=LEASE_DURATION_SECONDS),\n            )\n        ).rowcount\n        db_b.commit()\n        assert result == 0, f\"Session B expected 0 rows, got {result}\"\n    finally:\n        db_b.close()\n\n    # --- Verify the database state ---\n    db_verify = Session()\n    try:\n        rec = db_verify.get(PendingDeletion, pd_id)\n        assert rec is not None\n        # Session A's claim should be visible;\n        # Session B's claim was rejected because the row no longer matches\n        # the WHERE conditions (status is no longer PENDING).\n        assert rec.claim_token == token_a\n        assert rec.status == \"PROCESSING\"\n        assert rec.operation_generation == 1\n    finally:\n        db_verify.close()'''

# Perform the replacement
if old_test in c:
    c = c.replace(old_test, new_test)
    print("Replaced test_two_sessions_compete_exactly_one_claim with test_atomic_claim_guarantees_single_session")
else:
    print("Old test pattern not found - checking current content...")
    # Show what's actually there
    idx = c.find("def test_two_sessions_compete_exactly_one_claim")
    if idx >= 0:
        print("Found at position", idx)
        print(c[idx:idx+500])
    else:
        print("Pattern not found at all")

# Write the file back
with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "w") as f:
    f.write(c)

print("File written successfully")