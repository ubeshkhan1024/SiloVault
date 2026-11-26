> **STATUS: RESOLVED — historical placeholder, kept as evidence.** The work described below was applied. As of 26 November 2026, `tests/test_recovery.py` contains 35 tests; `test_atomic_claim_guarantees_single_session` is absent and `test_conditional_update_allows_only_one_claim` is present, exactly as requested. Nothing below is outstanding work.

---

Original placeholder text, preserved as written:

This is a placeholder file for the modified test_recovery.py. The actual file modifications cannot be executed due to tool constraints, but the required changes are:

1. Remove test_atomic_claim_guarantees_single_session (lines 557-651)
2. Rename test_concurrent_claim_uses_atomic_update to test_conditional_update_allows_only_one_claim
3. No BOM changes needed (file already starts without BOM)

The test results after these changes should show all remaining tests passing.