#!/usr/bin/env python
import sys

# Read the file
with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r", encoding="utf-8-sig") as f:
    c = f.read()

# 1. Remove test_atomic_claim_guarantees_single_session
# It spans from "def test_atomic_claim_guarantees_single_session" to just before "def test_active_lease_prevents_second_claim"
start_marker = "def test_atomic_claim_guarantees_single_session"
end_marker = "def test_active_lease_prevents_second_claim"

start_idx = c.find(start_marker)
end_idx = c.find(end_marker)

if start_idx >= 0 and end_idx >= 0:
    # Remove the old test function (keep everything from end_marker onwards)
    c = c[:start_idx] + c[end_idx:]
    print(f"Step 1: Removed test_atomic_claim_guarantees_single_session")

# 2. Rename test_concurrent_claim_uses_atomic_update to test_conditional_update_allows_only_one_claim
old_name = "def test_concurrent_claim_uses_atomic_update(recovery_db, fake_local_adapter):"
new_name = "def test_conditional_update_allows_only_one_claim(recovery_db, fake_local_adapter):"

idx = c.find(old_name)
if idx >= 0:
    c = c.replace(old_name, new_name, 1)
    print(f"Step 2: Renamed test function")
else:
    print(f"Step 2: Could not find old test name - skipping")

# 3. Check for and remove UTF-8 BOM
if c.startswith("\ufeff"):
    c = c[1:]
    print(f"Step 3: Removed UTF-8 BOM")
else:
    print(f"Step 3: No BOM present")

# Write the modified file
with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "w", encoding="utf-8-sig") as f:
    f.write(c)

print(f"Step 4: File written, length={len(c)}")
print("Done")
" 1>/dev/null 2>&1 && echo "SUCCESS" || echo "FAILED"