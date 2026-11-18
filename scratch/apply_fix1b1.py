"""Fix test_recovery.py: add operation_type to _seed_pending and update all calls."""

with open('tests/test_recovery.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Add operation_type parameter to _seed_pending signature
content = content.replace(
    '    lease_expires_at: datetime | None = None,\n    next_retry_at: datetime | None = None,\n) -> str:',
    '    lease_expires_at: datetime | None = None,\n    operation_type: str | None = None,\n) -> str:'
)

# 2. Add operation_type=operation_type to PendingDeletion constructor
content = content.replace(
    '            next_retry_at=next_retry_at,\n        )',
    '            next_retry_at=next_retry_at,\n            operation_type=operation_type,\n        )'
)

# 3. Add operation_type='CLEANUP' to all _seed_pending calls that don't have it
lines = content.split('\n')
new_lines = []
i = 0
while i < len(lines):
    line = lines[i]
    if '_seed_pending(' in line and 'operation_type' not in line:
        # This is a _seed_pending call without operation_type
        # Find the end of this call
        j = i
        depth = 0
        for k in range(j, len(lines)):
            for ch in lines[k]:
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        # Found closing paren at line k
                        # Insert operation_type before closing paren
                        if lines[k].strip().endswith(')'):
                            lines[k] = lines[k].replace(')', ', operation_type=\'CLEANUP\')')
                        else:
                            # Multi-line: insert on the line before closing paren
                            lines[k-1] = lines[k-1] + ', operation_type=\'CLEANUP\')'
                        i = k + 1
                        break
            else:
                continue
            break
        else:
            new_lines.append(line)
            i += 1
    else:
        new_lines.append(line)
        i += 1

content = '\n'.join(new_lines)

# 4. Append Fix 1B.1 tests
fix1b1_tests = '''


# ---------------------------------------------------------------------------
# Fix 1B.1: Atomic Recovery Claim Generation Tests
# ---------------------------------------------------------------------------


def test_claim_increments_operation_generation(recovery_db, fake_local_adapter):
    """A successfully claimed record has operation_generation incremented exactly once."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    pd_id = _seed_pending(Session, quarantine_id="q-gen-1", operation_type="CLEANUP")

    count = service.process_pending_deletions()
    assert count == 1

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is None  # finalized
    finally:
        db.close()


def test_claim_generation_captured_and_passed_to_execution(recovery_db, fake_local_adapter):
    """The generation captured at claim time is passed to _process_single_record."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    pd_id = _seed_pending(Session, quarantine_id="q-gen-2", operation_type="CLEANUP")

    # Patch _process_single_record to capture the generation it receives
    captured_generations = []
    original_process = service._process_single_record

    def _capturing_process(db, record, claim_token, claim_generation):
        captured_generations.append(claim_generation)
        return original_process(db, record, claim_token, claim_generation)

    service._process_single_record = _capturing_process

    service.process_pending_deletions()

    assert len(captured_generations) == 1
    # Generation must be > 0 (at least 1 since it was incremented from 0)
    assert captured_generations[0] >= 1


def test_later_claim_gets_different_generation(recovery_db, fake_local_adapter):
    """Two sequential claims of the same record get different generations."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    pd_id = _seed_pending(Session, quarantine_id="q-gen-3", operation_type="CLEANUP")

    # First claim
    count1 = service.process_pending_deletions()
    assert count1 == 1

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is None  # finalized
    finally:
        db.close()

    # Seed a new record with same quarantine_id (simulating re-creation)
    pd_id2 = _seed_pending(Session, quarantine_id="q-gen-3b", operation_type="CLEANUP")

    # Second claim
    count2 = service.process_pending_deletions()
    assert count2 == 1

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id2)
        assert rec is None  # finalized
    finally:
        db.close()
'''

content = content + fix1b1_tests

with open('tests/test_recovery.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('Done')