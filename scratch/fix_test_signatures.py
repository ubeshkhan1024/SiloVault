"""Fix test mock signatures for generation fencing repair."""

path = r'c:\Projects\SiloVault\tests\test_recovery.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# Fix _tampered_finalize signature + call
old1 = '''    def _tampered_finalize(db, record, claim_token):
        # Steal claim in DB before finalizing
        rec = db.get(PendingDeletion, record.id)
        rec.claim_token = "thief-token"
        db.commit()
        return original_finalize(db, record, claim_token)'''
new1 = '''    def _tampered_finalize(db, record, claim_token, claim_generation):
        # Steal claim in DB before finalizing
        rec = db.get(PendingDeletion, record.id)
        rec.claim_token = "thief-token"
        db.commit()
        return original_finalize(db, record, claim_token, claim_generation)'''

# Fix _tampered_schedule signature + call
old2 = '''    def _tampered_schedule(db, record, claim_token, error_msg, transient=True, retry_after=None):
        rec = db.get(PendingDeletion, record.id)
        rec.claim_token = "other-token"
        db.commit()
        return original_schedule(db, record, claim_token, error_msg, transient, retry_after)'''
new2 = '''    def _tampered_schedule(db, record, claim_token, claim_generation, error_msg, transient=True, retry_after=None):
        rec = db.get(PendingDeletion, record.id)
        rec.claim_token = "other-token"
        db.commit()
        return original_schedule(db, record, claim_token, claim_generation, error_msg, transient, retry_after)'''

if old1 not in content:
    print("ERROR: finalize mock not found")
    exit(1)
content = content.replace(old1, new1, 1)

if old2 not in content:
    print("ERROR: schedule mock not found")
    exit(1)
content = content.replace(old2, new2, 1)

print("Mock signatures fixed")
