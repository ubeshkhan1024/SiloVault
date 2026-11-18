import re

with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r", encoding="utf-8") as f:
    content = f.read()

# Map each test to its original token
# We'll apply fixes by matching the pattern around each t.start()
# The 5 failing tests and their original tokens:
fixes = [
    # Fix 1: test_expired_lease_allows_new_claim - old-dead-token
    {
        "original_token": "old-dead-token",
        "pattern": r't\.start\(\s*\)\n\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        "replacement": r't.start()\n\n    original_token = "old-dead-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n            time.sleep(0.05)\n        else:'
    },
    # Fix 2: test_new_claim_gets_different_token_and_incremented_generation - stale-token
    {
        "original_token": "stale-token",
        "pattern": r't\.start\(\s*\)\n\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        "replacement": r't.start()\n\n    original_token = "stale-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n            time.sleep(0.05)\n        else:'
    },
    # Fix 3: test_null_lease_can_be_reclaimed - old-null-token
    {
        "original_token": "old-null-token",
        "pattern": r't\.start\(\s*\)\n\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        "replacement": r't.start()\n\n    original_token = "old-null-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n            time.sleep(0.05)\n        else:'
    },
    # Fix 4: test_null_lease_new_token_differs - old-null-token
    {
        "original_token": "old-null-token",
        "pattern": r't\.start\(\s*\)\n\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        "replacement": r't.start()\n\n    original_token = "old-null-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n            time.sleep(0.05)\n        else:'
    },
    # Fix 5: test_null_lease_generation_increments_once - gen-token
    {
        "original_token": "gen-token",
        "pattern": r't\.start\(\s*\)\n\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        "replacement": r't.start()\n\n    original_token = "gen-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n            time.sleep(0.05)\n        else:'
    },
]

# Apply each fix
applied = 0
for i, fix in enumerate(fixes):
    if re.search(fix["pattern"], content, re.DOTALL):
        content = re.sub(fix["pattern"], fix["replacement"], content, flags=re.DOTALL)
        applied += 1
        print(f"Fix {i+1}: Applied (token={fix['original_token']})")
    else:
        print(f"Fix {i+1}: Pattern NOT found (token={fix['original_token']})")

# Write the file back
with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "w", encoding="utf-8") as f:
    f.write(content)

print(f"\nTotal fixes applied: {applied}")
print("File written successfully")