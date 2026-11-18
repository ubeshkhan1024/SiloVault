import re

with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r", encoding="utf-8") as f:
    content = f.read()

# Search for the exact pattern with 4 spaces before t.start()
pattern = r'    t\.start\(\s*\)\n    try:\n        # Wait for claim to be visible in DB\n    deadline = time\.time\(\) \+ 10\n    while time\.time\(\) < deadline:\n        db_check = Session\(\)\n        try:\n            rec = db_check\.get\(PendingDeletion, pd_id\)\n        if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\n            break\n    finally:\n        db_check\.close\(\)\n    time\.sleep\(0\.05\)\n    else:'

matches = list(re.finditer(pattern, content))
print(f'Found {len(matches)} matches')
for i, m in enumerate(matches):
    print(f'Match {i+1} at position {m.start()}-{m.end()}:')
    print(repr(content[m.start():m.end()+100]))