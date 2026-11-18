#!/usr/bin/env python
import re

with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r", encoding="utf-8") as f:
    content = f.read()

# The 5 fixes - each replaces the pattern between t.start() and claim_token is not None
# The common pattern (271 chars) is:
# t.start()\n\n    try:\n\n        # Wait for claim to be visible in DB\n\n        deadline = time.time() + 10\n\n        while time.time() < deadline:\n\n            db_check = Session()\n\n            try:\n\n                rec = db_check.get(PendingDeletion, pd_id)\n\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token is not None:\n\n                    break\n\n            finally:\n\n        db_check.close()\n\n    time.sleep(0.05)\n\n    else:

# We want to change to:
# t.start()\n\n    original_token = "..."\n\n    try:\n\n        # Wait for claim to be visible in DB\n\n        deadline = time.time() + 10\n\n        while time.time() < deadline:\n\n            db_check = Session()\n\n            try:\n\n                rec = db_check.get(PendingDeletion, pd_id)\n\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n\n                    break\n\n            finally:\n\n        db_check.close()\n\n    time.sleep(0.05)\n\n    else:

# Fix 1: test_expired_lease_allows_new_claim - old-dead-token
fix1_pattern = r'''t\.start\(\)\\n\\n    try:\\n\\n        # Wait for claim to be visible in DB\\n\\n        deadline = time\.time\(\) \+ 10\\n\\n        while time\.time\(\) < deadline:\\n\\n            db_check = Session\(\)\\n\\n            try:\\n\\n                rec = db_check\.get\(PendingDeletion, pd_id\)\\n\\n                if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:\\n\\n                    break'''

fix1_replacement = '''t.start()\n\n    original_token = "old-dead-token"\n\n    try:\n        # Wait for claim to be visible in DB\n        deadline = time.time() + 10\n        while time.time() < deadline:\n            db_check = Session()\n            try:\n                rec = db_check.get(PendingDeletion, pd_id)\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token != original_token:\n                    break\n            finally:\n                db_check.close()\n        else:'''

# Check if pattern exists
print("Fix 1 pattern in content:", bool(re.search(fix1_pattern, content, re.DOTALL)))

# Actually let me use a simpler approach - just replace the specific string
# Find 'rec.claim_token is not None' and replace with 'rec.claim_token != original_token'
# But we need to add the original_token line first

# Let me try replacing the entire block for each test

# The approach: match from t.start() to the end of the while loop, and replace
# But we need different tokens for each test

# Let me just do string replacement of the specific condition first
content_new = content

# Fix 1: Replace 'rec.claim_token is not None' with 'rec.claim_token != original_token' 
# but only in the context of the waiting loop
# This is tricky because the same string appears in multiple places

# Let me try a different approach - use the known positions
# We know the t.start() positions and can insert/original_token there

# Actually, let me just try the simplest possible replacement:
# Replace the first occurrence of the specific pattern

# Print what we're looking for
search_text = 'rec.claim_token is not None'
positions = []
idx = 0
while True:
    idx = content.find(search_text, idx)
    if idx == -1:
        break
    positions.append(idx)
    idx += 1
print(f'Found {len(positions)} occurrences of "rec.claim_token is not None"')
for i, p in enumerate(positions[:5]):
    print(f'  Position {p}: context = {repr(content[p-30:p+30])}')
"