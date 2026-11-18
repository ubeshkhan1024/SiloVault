import pathlib

path = pathlib.Path('tests/test_recovery.py')
content = path.read_text(encoding='utf-8')

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
    operation_type: str | None = None,
, operation_type='CLEANUP') -> str:
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
    operation_type: str | None = None,
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

if old_func in content:
    content = content.replace(old_func, new_func)
    path.write_text(content, encoding='utf-8')
    print('Fixed _seed_pending function')
else:
    print('ERROR: Could not find the broken function')
    # Debug: show lines around 124-160
    lines = content.split('\n')
    for i in range(123, min(160, len(lines))):
        print(f'{i+1}: {repr(lines[i])}')
