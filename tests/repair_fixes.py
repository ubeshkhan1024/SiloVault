#!/usr/bin/env python
import re

with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r", encoding="utf-8") as f:
    content = f.read()

# The 5 fixes - each replaces the pattern t.start()...rec.claim_token is not None
# with the fixed version that has original_token and the != condition

fixes = [
    # Fix 1: test_expired_lease_allows_new_claim - old-dead-token
    (
        r't\.start\(\s*\)\n\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        r't.start()\n\n    original_token = "old-dead-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n            time.sleep(0.05)\n        else:'
    ),
    # Fix 2: test_new_claim_gets_different_token_and_incremented_generation - stale-token
    (
        r't\.start\(\s*\)\n\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        r't.start()\n\n    original_token = "stale-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n            time.sleep(0.05)\n        else:'
    ),
    # Fix 3: test_null_lease_can_be_reclaimed - old-null-token
    (
        r't\.start\(\s*\)\n\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        r't.start()\n\n    original_token = "old-null-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n            time.sleep(0.05)\n        else:'
    ),
    # Fix 4: test_null_lease_new_token_differs - old-null-token
    (
        r't\.start\(\s*\)\n\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        r't.start()\n\n    original_token = "old-null-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n            time.sleep(0.05)\n        else:'
    ),
    # Fix 5: test_null_lease_generation_increments_once - gen-token
    (
        r't\.start\(\s*\)\n\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        r't.start()\n\n    original_token = "gen-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n            time.sleep(0.05)\n        else:'
    ),
]

applied = 0
for i, (pattern, replacement) in enumerate(fixes):
    if re.search(pattern, content, re.DOTALL):
        content = re.sub(pattern, replacement, content, flags=re.DOTALL)
        applied += 1
        print(f"Fix {i+1} applied successfully")
    else:
        print(f"Fix {i+1} - pattern not found")

# Write the file back
with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "w", encoding="utf-8") as f:
    f.write(content)

print(f"\nTotal fixes applied: {applied}")