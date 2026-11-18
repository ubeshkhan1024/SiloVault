import pathlib

path = pathlib.Path('tests/test_recovery.py')
content = path.read_text(encoding='utf-8')

# Fix the two multiline calls that have bad formatting
# Pattern: , operation_type='CLEANUP')\n    )\n    count = service...
# Should be: quarantine_id="q-expired",\n        operation_type="CLEANUP",
#     )

old1 = """    pd_id = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="old-dead-token",
        lease_expires_at=past_time,
        quarantine_id="q-expired",
    , operation_type='CLEANUP')"""

new1 = """    pd_id = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="old-dead-token",
        lease_expires_at=past_time,
        quarantine_id="q-expired",
        operation_type="CLEANUP",
    )"""

old2 = """    pd_id = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="active-worker-token",
        lease_expires_at=future_time,
        quarantine_id="q-active",
    , operation_type='CLEANUP')"""

new2 = """    pd_id = _seed_pending(
        Session,
        status="PROCESSING",
        claim_token="active-worker-token",
        lease_expires_at=future_time,
        quarantine_id="q-active",
        operation_type="CLEANUP",
    )"""

content = content.replace(old1, new1)
content = content.replace(old2, new2)
path.write_text(content, encoding='utf-8')
print('Done')
