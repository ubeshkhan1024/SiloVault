import re

with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r", encoding="utf-8") as f:
    content = f.read()

# Print the exact pattern we're searching for (Fix 1)
pattern = r't\.start\(\s*\)\n\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:'

print("Pattern:")
print(pattern)
print()

# Find t.start() in the content
idx = content.find('t.start()')
print(f"First t.start() at position: {idx}")
if idx >= 0:
    print("Content around t.start():")
    print(repr(content[idx:idx+200]))