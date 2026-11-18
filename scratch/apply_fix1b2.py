import re

path = r'c:\Projects\SiloVault\tests\test_recovery.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# Find and replace _seed_pending function
old_func = '''def _seed_pending(
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
        db.close()'''

new_func = '''def _seed_pending(
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
            operation_type=operation_type,
        )
        db.add(pd)
        db.commit()
        db.refresh(pd)
        return pd.id
    finally:
        db.close()'''

if old_func not in content:
    print("ERROR: old function not found in file")
    exit(1)

content = content.replace(old_func, new_func, 1)

# Append new test before the last blank lines
new_test = '''

def test_restore_operation_type_seedable(recovery_db, fake_local_adapter):
    """A RESTORE-record seeds successfully and is dispatched, not silently skipped."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    pd_id = _seed_pending(Session, quarantine_id="q-restore-1", operation_type="RESTORE")

    count = service.process_pending_deletions()
    # FakeAdapter.restore raises NotImplementedError -> transient retry; not counted as success
    assert count == 0

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.operation_type == "RESTORE"
        assert rec.status == "PENDING"
        assert rec.retry_count == 1
    finally:
        db.close()
'''

# Append at end of file
content = content.rstrip() + new_test

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Edit successful")