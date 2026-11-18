import re

# Read the file
with open('tests/test_recovery.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Replace cleanup test calls with operation_type="CLEANUP"
replacements = [
    (r'pd_id = _seed_pending\(Session, provider="LOCAL", quarantine_id="q-local-1"\)', 
     'pd_id = _seed_pending(Session, provider="LOCAL", quarantine_id="q-local-1", operation_type="CLEANUP")'),
    (r'pd_id = _seed_pending\(Session, provider="GOOGLE_DRIVE", quarantine_id="drive-q-99"\)', 
     'pd_id = _seed_pending(Session, provider="GOOGLE_DRIVE", quarantine_id="drive-q-99", operation_type="CLEANUP")'),
    (r'pd_id = _seed_pending\(Session, quarantine_id="already-gone"\)', 
     'pd_id = _seed_pending(Session, quarantine_id="already-gone", operation_type="CLEANUP")'),
    (r'pd_id = _seed_pending\(\s*Session,\s*status="PROCESSING",\s*claim_token="old-dead-token",\s*lease_expires_at=past_time,\s*quarantine_id="q-expired",\s*\)', 
     'pd_id = _seed_pending(\n        Session,\n        status="PROCESSING",\n        claim_token="old-dead-token",\n        lease_expires_at=past_time,\n        quarantine_id="q-expired",\n        operation_type="CLEANUP",\n    )'),
    (r'pd_id = _seed_pending\(Session, status="PROCESSING", claim_token="active-worker-token", lease_expires_at=future_time, quarantine_id="q-active"\)', 
     'pd_id = _seed_pending(Session, status="PROCESSING", claim_token="active-worker-token", lease_expires_at=future_time, quarantine_id="q-active", operation_type="CLEANUP")'),
    (r'pd_id = _seed_pending\(Session, quarantine_id="q-transient"\)', 
     'pd_id = _seed_pending(Session, quarantine_id="q-transient", operation_type="CLEANUP")'),
    (r'pd_id = _seed_pending\(Session, quarantine_id="q-ratelimit"\)', 
     'pd_id = _seed_pending(Session, quarantine_id="q-ratelimit", operation_type="CLEANUP")'),
    (r'pd_id = _seed_pending\(Session, retry_count=MAX_RETRIES - 1, quarantine_id="q-maxretries"\)', 
     'pd_id = _seed_pending(Session, retry_count=MAX_RETRIES - 1, quarantine_id="q-maxretries", operation_type="CLEANUP")'),
    (r'pd_id = _seed_pending\(Session, quarantine_id="q-authfail"\)', 
     'pd_id = _seed_pending(Session, quarantine_id="q-authfail", operation_type="CLEANUP")'),
    (r'pd_id = _seed_pending\(Session, quarantine_id="q-permfail"\)', 
     'pd_id = _seed_pending(Session, quarantine_id="q-permfail", operation_type="CLEANUP")'),
    (r'pd_id = _seed_pending\(Session, next_retry_at=future_retry, quarantine_id="q-future"\)', 
     'pd_id = _seed_pending(Session, next_retry_at=future_retry, quarantine_id="q-future", operation_type="CLEANUP")'),
    (r'pd_id = _seed_pending\(Session, quarantine_id="q-stolen"\)', 
     'pd_id = _seed_pending(Session, quarantine_id="q-stolen", operation_type="CLEANUP")'),
    (r'pd_id = _seed_pending\(Session, quarantine_id="q-retry-stolen"\)', 
     'pd_id = _seed_pending(Session, quarantine_id="q-retry-stolen", operation_type="CLEANUP")'),
    (r'pd_id = _seed_pending\(Session, provider="LOCAL", quarantine_id="q-no-adapter"\)', 
     'pd_id = _seed_pending(Session, provider="LOCAL", quarantine_id="q-no-adapter", operation_type="CLEANUP")'),
    (r'pd_id = _seed_pending\(Session, quarantine_id="q-background-1"\)', 
     'pd_id = _seed_pending(Session, quarantine_id="q-background-1", operation_type="CLEANUP")'),
]

for pattern, replacement in replacements:
    content = re.sub(pattern, replacement, content, flags=re.MULTILINE | re.DOTALL)

# Write the file back
with open('tests/test_recovery.py', 'w', encoding='utf-8', newline='') as f:
    f.write(content)

print("File updated successfully")