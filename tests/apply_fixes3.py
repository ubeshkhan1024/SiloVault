import re

with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r", encoding="utf-8") as f:
    content = f.read()

# Map each t.start() occurrence to its test and original token
# Based on the analysis:
# t.start() positions: 25429, 28778, 41684, 44662, 47546
# Corresponding tests and tokens:
# 1. Position 25429 -> test_expired_lease_allows_new_claim -> old-dead-token
# 2. Position 28778 -> test_new_claim_gets_different_token_and_incremented_generation -> stale-token
# 3. Position 41684 -> test_null_lease_can_be_reclaimed -> old-null-token
# 4. Position 44662 -> test_null_lease_new_token_differs -> old-null-token
# 5. Position 47546 -> test_null_lease_generation_increments_once -> gen-token

fixes = [
    # Fix 1: Position 25429 - test_expired_lease_allows_new_claim
    {
        "start_pos": 25429,
        "original_token": "old-dead-token",
        # Pattern: t.start()\n followed by the try/wait loop
        "pattern": r't\.start\(\s*\)\n\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        "replacement": r't.start()\n\n    original_token = "old-dead-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n            time.sleep(0.05)\n        else:'
    },
    # Fix 2: Position 28778 - test_new_claim_gets_different_token_and_incremented_generation
    {
        "start_pos": 28778,
        "original_token": "stale-token",
        "pattern": r't\.start\(\s*\)\n\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        "replacement": r't.start()\n\n    original_token = "stale-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n            time.sleep(0.05)\n        else:'
    },
    # Fix 3: Position 41684 - test_null_lease_can_be_reclaimed
    {
        "start_pos": 41684,
        "original_token": "old-null-token",
        "pattern": r't\.start\(\s*\)\n\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        "replacement": r't.start()\n\n    original_token = "old-null-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n            time.sleep(0.05)\n        else:'
    },
    # Fix 4: Position 44662 - test_null_lease_new_token_differs
    {
        "start_pos": 44662,
        "original_token": "old-null-token",
        "pattern": r't\.start\(\s*\)\n\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        "replacement": r't.start()\n\n    original_token = "old-null-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n            time.sleep(0.05)\n        else:'
    },
    # Fix 5: Position 47546 - test_null_lease_generation_increments_once
    {
        "start_pos": 47546,
        "original_token": "gen-token",
        "pattern": r't\.start\(\s*\)\n\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        "replacement": r't.start()\n\n    original_token = "gen-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n            time.sleep(0.05)\n        else:'
    },
]

# Apply each fix by position
applied = 0
for i, fix in enumerate(fixes):
    # Check if we're at the right position
    if content[fix["start_pos"]:fix["start_pos"]+20] == 't.start()':
        # Apply the regex replacement
        if re.search(fix["pattern"], content, re.DOTALL):
            content = re.sub(fix["pattern"], fix["replacement"], content, flags=re.DOTALL)
            applied += 1
            print(f"Fix {i+1}: Applied at position {fix['start_pos']} (token={fix['original_token']})")
        else:
            print(f"Fix {i+1}: Pattern NOT found at position {fix['start_pos']} (token={fix['original_token']})")
    else:
        print(f"Fix {i+1}: Wrong position {fix['start_pos']} - expected t.start(), got: {repr(content[fix['start_pos']:fix['start_pos']+20])}")

# Write the file back
with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "w", encoding="utf-8") as f:
    f.write(content)

print(f"\nTotal fixes applied: {applied}")
print("File written successfully")