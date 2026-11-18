import re

# Read the file
with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r", encoding="utf-8") as f:
    content = f.read()

# Define the fixes for each test
fixes = [
    # test_expired_lease_allows_new_claim - insert after t.start(), change condition
    {
        "pattern": r'(    t\.start\(\s*\)\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == \"PROCESSING\" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        "original_token": "old-dead-token",
        "replacement": r'''    t.start()

    original_token = "old-dead-token"

    try:
        # Wait for claim to be visible in DB
        deadline = time.time() + 10
        while time.time() < deadline:
            db_check = Session()
            try:
                rec = db_check.get(PendingDeletion, pd_id)
                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:
                    break
            finally:
                db_check.close()
            time.sleep(0.05)
        else:'''
    },
    # test_new_claim_gets_different_token_and_incremented_generation
    {
        "pattern": r'(    t\.start\(\s*\)\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == \"PROCESSING\" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        "original_token": "stale-token",
        "replacement": r'''    t.start()

    original_token = "stale-token"

    try:
        # Wait for claim to be visible in DB
        deadline = time.time() + 10
        while time.time() < deadline:
            db_check = Session()
            try:
                rec = db_check.get(PendingDeletion, pd_id)
                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:
                    break
            finally:
                db_check.close()
            time.sleep(0.05)
        else:'''
    },
    # test_null_lease_can_be_reclaimed
    {
        "pattern": r'(    t\.start\(\s*\)\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == \"PROCESSING\" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        "original_token": "old-null-token",
        "replacement": r'''    t.start()

    original_token = "old-null-token"

    try:
        # Wait for claim to be visible in DB
        deadline = time.time() + 10
        while time.time() < deadline:
            db_check = Session()
            try:
                rec = db_check.get(PendingDeletion, pd_id)
                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:
                    break
            finally:
                db_check.close()
            time.sleep(0.05)
        else:'''
    },
    # test_null_lease_new_token_differs
    {
        "pattern": r'(    t\.start\(\s*\)\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == \"PROCESSING\" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        "original_token": "old-null-token",
        "replacement": r'''    t.start()

    original_token = "old-null-token"

    try:
        # Wait for claim to be visible in DB
        deadline = time.time() + 10
        while time.time() < deadline:
            db_check = Session()
            try:
                rec = db_check.get(PendingDeletion, pd_id)
                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:
                    break
            finally:
                db_check.close()
            time.sleep(0.05)
        else:'''
    },
    # test_null_lease_generation_increments_once
    {
        "pattern": r'(    t\.start\(\s*\)\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == \"PROCESSING\" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:',
        "original_token": "gen-token",
        "replacement": r'''    t.start()

    original_token = "gen-token"

    try:
        # Wait for claim to be visible in DB
        deadline = time.time() + 10
        while time.time() < deadline:
            db_check = Session()
            try:
                rec = db_check.get(PendingDeletion, pd_id)
                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:
                    break
            finally:
                db_check.close()
            time.sleep(0.05)
        else:'''
    },
]

# Apply each fix
for i, fix in enumerate(fixes):
    match = re.search(fix["pattern"], content, re.DOTALL)
    if match:
        # Replace the pattern with the new code including original_token
        replacement = fix["replacement"]
        content = content[:match.start()] + replacement + content[match.end():]
        print(f"Fix {i+1} applied successfully")
    else:
        print(f"Fix {i+1} - pattern not found, trying alternative approach")

# Write the file back
with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "w", encoding="utf-8") as f:
    f.write(content)

print("File written successfully")