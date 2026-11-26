## SiloVault Repair Summary

> **HISTORICAL RECORD — repair worklist from an earlier session, preserved as written.** This file states changes that were *required at the time*; it is not a description of the current tree. The repairs were subsequently applied, so read the "Changes required" wording as past intent rather than outstanding work.
>
> Current state (26 November 2026): `tests/test_recovery.py` contains 35 tests (the 32 cited below is historical), `tests/test_recovery_backup.py` still exists on disk, and the `PendingDeletion`/`restore()` details described here are already in the code. No claim in this file was verified by a test run during the documentation pass.

### Task: Repair 5B - Verify Google Drive Parent Preservation

**File**: `backend/storage/google_drive.py`

**Changes required in `GoogleDriveAdapter.restore()`**:

1. ✅ Keep untrash operation exactly: `body={"trashed": False}`
2. Change metadata fields to include `parents`:
   - From: `fields="id,name,mimeType,size,modifiedTime,md5Checksum"`
   - To: `fields="id,name,mimeType,size,modifiedTime,md5Checksum,parents"`
3. Add to provider_metadata dictionary:
   - `"parent_ids": response.get("parents", [])`
4. ✅ Preserve existing `response.get("md5Checksum")` handling
5. ✅ Do not add `addParents` or `removeParents`

---

### Task: Repair 2 - Strengthen Recovery Claim Tests

**File**: `tests/test_recovery.py`

**5 tests need the fix** (polling condition bug where `rec.claim_token is not None` 
immediately evaluates to True on the seeded token):

| Test Name | Original Token | Fix Required |
|-----------|---------------|-------------|
| `test_expired_lease_allows_new_claim` | `old-dead-token` | Insert `original_token = "old-dead-token"` after `t.start()`, change condition to `rec.claim_token != original_token` |
| `test_new_claim_gets_different_token_and_incremented_generation` | `stale-token` | Insert `original_token = "stale-token"` after `t.start()`, change condition to `rec.claim_token != original_token` |
| `test_null_lease_can_be_reclaimed` | `old-null-token` | Insert `original_token = "old-null-token"` after `t.start()`, change condition to `rec.claim_token != original_token` |
| `test_null_lease_new_token_differs` | `old-null-token` | Insert `original_token = "old-null-token"` after `t.start()`, change condition to `rec.claim_token != original_token` |
| `test_null_lease_generation_increments_once` | `gen-token` | Insert `original_token = "gen-token"` after `t.start()`, change condition to `rec.claim_token != original_token` |

**Fix pattern** (applied to each test's while loop):
```
- if rec is not None and rec.status == "PROCESSING" and rec.claim_token is not None:
+ if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:
+ (after t.start(): original_token = "...")
```

**Current test status**:
- Backup `tests/test_recovery_backup.py`: All 32 tests pass
- Original state: 5 tests fail, 27 pass
- Repair 1 (PendingDeletion model): Already completed

---

### Task: Repair 1 - Align PendingDeletion model with migration

**File**: `backend/models/pending_deletion.py`

**Change**: 
- From: `Mapped[Optional[str]]` with `nullable=True`
- To: `Mapped[str]` with `nullable=False`

**Status**: Already completed.

---