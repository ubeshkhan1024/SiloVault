import pathlib

path = pathlib.Path('tests/test_recovery.py')
content = path.read_text(encoding='utf-8')

new_tests = '''# ---------------------------------------------------------------------------
# Operation Type Dispatch Tests
# ---------------------------------------------------------------------------

def test_missing_operation_type_skipped(recovery_db, fake_local_adapter):
    """A record with operation_type=None is skipped before provider lookup or action."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    pd_id = _seed_pending(Session, operation_type=None)

    count = service.process_pending_deletions()
    assert count == 0
    assert fake_local_adapter.cleaned_ids == []

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.status == "PENDING"
    finally:
        db.close()


def test_unknown_operation_type_skipped(recovery_db, fake_local_adapter):
    """A record with an unrecognised operation_type is skipped before provider lookup or action."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    pd_id = _seed_pending(Session, operation_type="INVALID_OP")

    count = service.process_pending_deletions()
    assert count == 0
    assert fake_local_adapter.cleaned_ids == []

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.status == "PENDING"
    finally:
        db.close()


def test_explicit_cleanup_dispatch(recovery_db, fake_local_adapter):
    """A record with operation_type='CLEANUP' dispatches to cleanup and finalizes."""
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    pd_id = _seed_pending(Session, quarantine_id="q-cleanup-dispatch", operation_type="CLEANUP")

    count = service.process_pending_deletions()
    assert count == 1
    assert fake_local_adapter.cleaned_ids == ["q-cleanup-dispatch"]

    db = Session()
    try:
        assert db.get(PendingDeletion, pd_id) is None
    finally:
        db.close()


def test_explicit_restore_dispatch(recovery_db, fake_local_adapter):
    """A record with operation_type='RESTORE' dispatches to restore and finalizes.

    This only verifies the dispatch path and finalization; it does not claim
    that the full restore workflow (metadata reconstruction) is safe.
    """
    Session = recovery_db["Session"]
    service = recovery_db["service"]

    # Patch adapter.restore to succeed
    fake_local_adapter.restore = lambda qid: None  # type: ignore[assignment]

    pd_id = _seed_pending(Session, quarantine_id="q-restore-dispatch", operation_type="RESTORE")

    count = service.process_pending_deletions()
    assert count == 1

    db = Session()
    try:
        assert db.get(PendingDeletion, pd_id) is None
    finally:
        db.close()


def test_restore_storage_unsupported_error_is_permanent_failure(recovery_db, fake_local_adapter):
    """StorageUnsupportedError during RESTORE is a permanent failure, never falls back to cleanup."""
    from backend.storage.errors import StorageUnsupportedError

    Session = recovery_db["Session"]
    service = recovery_db["service"]

    # Patch adapter.restore to raise StorageUnsupportedError
    fake_local_adapter.restore = lambda qid: (_ for _ in ()).throw(StorageUnsupportedError("Restore not supported", provider="LOCAL"))  # type: ignore[assignment]

    pd_id = _seed_pending(Session, quarantine_id="q-restore-unsupported", operation_type="RESTORE")

    count = service.process_pending_deletions()
    assert count == 0
    # Cleanup should NOT have been called
    assert fake_local_adapter.cleaned_ids == []

    db = Session()
    try:
        rec = db.get(PendingDeletion, pd_id)
        assert rec is not None
        assert rec.status == "FAILED"
        assert "not supported" in rec.last_error.lower()
    finally:
        db.close()


'''

marker = "# ---------------------------------------------------------------------------\n# Background Worker & Lifespan Integration Tests\n# ---------------------------------------------------------------------------\n"

if marker in content:
    content = content.replace(marker, new_tests + marker)
    path.write_text(content, encoding='utf-8')
    print('Done')
else:
    print('Marker not found!')
    # Debug: show what we have
    lines = content.split('\n')
    for i, line in enumerate(lines):
        if 'Background Worker' in line:
            print(f"Found at line {i}: {repr(line)}")
            print(f"Context: {repr('\n'.join(lines[max(0,i-2):i+3]))}")
