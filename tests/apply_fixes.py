import re

# Read the file in binary mode to avoid encoding issues
with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "rb") as f:
    content_bytes = f.read()

# Try UTF-8 with fallback
try:
    content = content_bytes.decode("utf-8")
except:
    content = content_bytes.decode("latin-1")

# The five patterns to replace - each has the t.start() followed by while loop checking claim_token is not None
# We need to insert original_token = "..." after t.start() and change the condition

fixes = [
    # Fix 1: test_expired_lease_allows_new_claim - original token is old-dead-token
    (
        r'    t\.start\(\s*\)\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        'old-dead-token',
        r'    t.start()\n\n    original_token = "old-dead-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n            time.sleep(0.05)\n        else:'
    ),
    # Fix 2: test_new_claim_gets_different_token_and_incremented_generation - original token is stale-token
    (
        r'    t\.start\(\s*\)\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        'stale-token',
        r'    t.start()\n\n    original_token = "stale-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n            time.sleep(0.05)\n        else:'
    ),
    # Fix 3: test_null_lease_can_be_reclaimed - original token is old-null-token
    (
        r'    t\.start\(\s*\)\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        'old-null-token',
        r'    t.start()\n\n    original_token = "old-null-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n            time.sleep(0.05)\n        else:'
    ),
    # Fix 4: test_null_lease_new_token_differs - original token is old-null-token
    (
        r'    t\.start\(\s*\)\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        'old-null-token',
        r'    t.start()\n\n    original_token = "old-null-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n            time.sleep(0.05)\n        else:'
    ),
    # Fix 5: test_null_lease_generation_increments_once - original token is gen-token
    (
        r'    t\.start\(\s*\)\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        'gen-token',
        r'    t.start()\n\n    original_token = "gen-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n            time.sleep(0.05)\n        else:'
    ),
]

# Apply each fix
applied = 0
for i, (pattern, original_token, replacement) in enumerate(fixes):
    if re.search(pattern, content, re.DOTALL):
        content = re.sub(pattern, replacement, content, flags=re.DOTALL)
        applied += 1
        print(f"Fix {i+1}: Applied (original_token={original_token})")
    else:
        print(f"Fix {i+1}: Pattern NOT found (original_token={original_token})")

# Write the file back
with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "w", encoding="utf-8") as f:
    f.write(content)

print(f"Total fixes applied: {applied}")
print("File written successfully")