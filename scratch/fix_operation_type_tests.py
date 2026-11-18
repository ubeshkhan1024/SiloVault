import pathlib

path = pathlib.Path('tests/test_recovery.py')
content = path.read_text(encoding='utf-8')

# Fix test_missing_operation_type_skipped: update docstring and assertion
old1 = '''def test_missing_operation_type_skipped(recovery_db, fake_local_adapter):
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
        db.close()'''

new1 = '''def test_missing_operation_type_skipped(recovery_db, fake_local_adapter):
    """A record with operation_type=None is never claimed and remains PENDING."""
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
        db.close()'''

# Fix test_unknown_operation_type_skipped: update docstring and assertion
old2 = '''def test_unknown_operation_type_skipped(recovery_db, fake_local_adapter):
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
        db.close()'''

new2 = '''def test_unknown_operation_type_skipped(recovery_db, fake_local_adapter):
    """A record with an unrecognised operation_type is never claimed and remains PENDING."""
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
        db.close()'''

if old1 in content:
    content = content.replace(old1, new1)
    print('Fixed test_missing_operation_type_skipped')
else:
    print('ERROR: Could not find test_missing_operation_type_skipped')

if old2 in content:
    content = content.replace(old2, new2)
    print('Fixed test_unknown_operation_type_skipped')
else:
    print('ERROR: Could not find test_unknown_operation_type_skipped')

path.write_text(content, encoding='utf-8')
