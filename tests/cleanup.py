#!/usr/bin/env python
with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r", encoding="utf-8-sig") as f:
    c = f.read()

# Step 1: Remove test_atomic_claim_guarantees_single_session (lines 557-677)
# The function is from "def test_atomic_claim_guarantees_single_session" to just before "def test_expired_lease_allows_new_claim"
old_section = """def test_atomic_claim_guarantees_single_session(recovery_db, fake_local_adapter):
    \"\"\"Two independent sessions compete for the same eligible record.

    Exactly one session obtains the claim via the conditional UPDATE that
    matches the production eligibility conditions (status, lease, next_retry_at,
    operation_type). The second session observes rowcount == 0 and the
    database retains the first session's claim token, PROCESSING status, and
    operation_generation == 1.
    \"\"\"\n    from sqlalchemy import update\n    import uuid\n    from datetime import datetime, timedelta\n    from backend.services.recovery import LEASE_DURATION_SECONDS\n\n    Session = recovery_db["Session"]\n    service = recovery_db["service"]\n\n    # Seed a single PENDING record.\n    pd_id = _seed_pending(Session, quarantine_id="q-atomic-1")\n\n    # --- Session A: conditional claim UPDATE ---\n    now = datetime.now(timezone.utc)\n    token_a = str(uuid.uuid4())\n    db_a = Session()\n    try:\n        result = db_a.execute(\n            update(PendingDeletion)\n            .where(\n                PendingDeletion.id == pd_id,\n                PendingDeletion.status == "PENDING",\n                PendingDeletion.next_retry_at.is_(None),\n                PendingDeletion.operation_type.in_([\"CLEANUP\", \"RESTORE\"]),\n            )\n            .values(\n                status="PROCESSING",\n                claim_token=token_a,\n                operation_generation=PendingDeletion.operation_generation + 1,\n                processing_started_at=now,\n                lease_expires_at=now + timedelta(seconds=LEASE_DURATION_SECONDS),\n            )
        ).rowcount\n        db_a.commit()\n        assert result == 1, f\"Session A expected 1 row, got {result}\"
    finally:\n        db_a.close()\n\n    # Commit Session A so the claim is visible to Session B.\n    db_a_verify = Session()\n    try:\n        rec = db_a_verify.get(PendingDeletion, pd_id)\n        assert rec is not None\n        assert rec.status == "PROCESSING"\n        assert rec.claim_token == token_a\n        assert rec.operation_generation == 1\n    finally:\n        db_a_verify.close()\n\n    # --- Session B: attempt the same conditional claim ---\n    db_b = Session()\n    try:\n        result = db_b.execute(\n            update(PendingDeletion)\n            .where(\n                PendingDeletion.id == pd_id,\n                PendingDeletion.status == "PENDING",\n                PendingDeletion.next_retry_at.is_(None),\n                PendingDeletion.operation_type.in_([\"CLEANUP\", \"RESTORE\"]),\n            )\n            .values(\n                status="PROCESSING",\n                claim_token=str(uuid.uuid4()),\n                operation_generation=PendingDeletion.operation_generation + 1,\n                processing_started_at=now,\n                lease_expires_at=now + timedelta(seconds=LEASE_DURATION_SECONDS),\n            )
        ).rowcount\n        db_b.commit()\n        assert result == 0, f\"Session B expected 0 rows, got {result}\"\n    finally:\n        db_b.close()\n\n    # --- Verify the database state ---\n    db_verify = Session()\n    try:\n        rec = db_verify.get(PendingDeletion, pd_id)\n        assert rec is not None\n        # Session A's claim should be visible;\n        # Session B's claim was rejected because the row no longer matches\n        # the WHERE conditions (status is no longer PENDING).\n        assert rec.claim_token == token_a\n        assert rec.status == "PROCESSING"\n        assert rec.operation_generation == 1\n    finally:\n        db_verify.close()"""

# Remove the old section - replace with nothing (just the newline before test_expired_lease_allows_new_claim)
# Find the position of the old test and the position of the new test
idx_old = c.find("def test_atomic_claim_guarantees_single_session")
idx_new = c.find("def test_expired_lease_allows_new_claim")

if idx_old >= 0 and idx_new >= 0:
    # Get the content after the old test (from idx_new onwards)
    after_old = c[idx_new:]
    # Check if removing the old test works
    c_new = c[:idx_old] + after_old
    print(f"Removed test_atomic_claim_guarantees_single_session")
    print(f"  Old test started at: {idx_old}")
    print(f"  Content after removal starts with: {c_new[idx_new-10:idx_new+50]}")
else:
    print(f"Could not find tests: old={idx_old >= 0}, new={idx_new >= 0}")

# Step 2: Rename test_concurrent_claim_uses_atomic_update to test_conditional_update_allows_only_one_claim
old_test_name = "def test_concurrent_claim_uses_atomic_update(recovery_db, fake_local_adapter):"
new_test_name = "def test_conditional_update_allows_only_one_claim(recovery_db, fake_local_adapter):"

idx_rename = c.find(old_test_name)
if idx_rename >= 0:
    # Just replace the function name
    c = c.replace(old_test_name, new_test_name, 1)  # Only replace first occurrence
    print(f"Renamed test_concurrent_claim_uses_atomic_update to test_conditional_update_allows_only_one_claim")
else:
    print(f"Could not find test_concurrent_claim_uses_atomic_update")

# Step 3: Remove UTF-8 BOM if present
# The file was opened with utf-8-sig, check if BOM exists at start
if c.startswith("\ufeff"):
    c = c[1:]  # Remove BOM
    print("Removed UTF-8 BOM")
else:
    print("No UTF-8 BOM found at start of file")

# Write the file back with utf-8-sig encoding to preserve proper encoding
with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "w", encoding="utf-8-sig") as f:
    f.write(c)

print("File written successfully")
print(f"File length: {len(c)}")