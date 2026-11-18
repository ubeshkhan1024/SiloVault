import re

with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r", encoding="utf-8") as f:
    content = f.read()

# The common pattern that follows t.start() in all 5 tests
# This matches: t.start()\n\n    try:\n\n        # Wait for claim to be visible in DB\n\n        deadline = time.time() + 10\n\n        while time.time() < deadline:\n\n            db_check = Session()\n\n            try:\n\n                rec = db_check.get(PendingDeletion, pd_id)\n\n                if rec is not None and rec.status == "PROCESSING" and rec.claim_token is not None:\n\n                    break\n\n            finally:\n\n        db_check.close()\n\n    time.sleep(0.05)\n\n    else:

# We'll replace this with a version that has original_token inserted and the condition changed
# The trick is to match the pattern and use a function to determine the token

def replace_match(m):
    # The match contains t.start() followed by the pattern
    # We need to insert original_token = "..." after t.start()
    # and change rec.claim_token is not None to rec.claim_token != original_token
    
    # Get the matched text
    matched = m.group(0)
    
    # Determine which test this is based on context (look backwards for the test name)
    # For now, we'll just replace all with a generic approach
    # Actually, let's just do a simple string replacement on the condition and insert token
    
    # Insert original_token = "..." after t.start()
    # The pattern is: t.start()\n\n
    # We want: t.start()\n\n    original_token = "...\"\n\n
    
    # But we need to know which token to use for each test
    # Let's look at what token is being checked in the is not None condition
    
    # Actually, the simplest approach: just replace the condition and add original_token
    # We'll do 5 separate targeted replacements
    
    return matched

# Instead of a complex regex, let's do 5 targeted string replacements
# Each test has the t.start() at a specific location, and we know the original token

# Fix 1: test_expired_lease_allows_new_claim - token is old-dead-token
# Find t.start() in the context of this test and replace
# Let's search for a unique marker near each t.start()

# Actually, let's use a different approach: replace the condition first, then insert the token

# Step 1: Replace the condition in all 5 places
# Pattern: rec.claim_token is not None -> rec.claim_token != original_token
# But we need to add the original_token line first

# Let me try a simpler approach - just replace the specific strings

# Find all occurrences of "rec.claim_token is not None" and replace them
# But we need to be careful not to replace the wrong ones

# Let's look at the context around each occurrence
import re

# Pattern to find the claim_token check
check_pattern = r'if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:'
check_matches = list(re.finditer(check_pattern, content))
print(f'Found {len(check_matches)} occurrences of "rec.claim_token is not None"')

for i, m in enumerate(check_matches):
    start = max(0, m.start() - 50)
    end = min(len(content), m.end() + 50)
    print(f'\nOccurrence {i+1} at position {m.start()}:')
    print(repr(content[start:end]))

# Now let's do the replacements
# For each test, we need to:
# 1. After t.start(), insert original_token = "..."
# 2. Change rec.claim_token is not None to rec.claim_token != original_token

# Since all 5 have the same structure after t.start(), let's do a batch replacement
# We'll match: t.start() followed by the try block, and replace the whole thing

# Actually, the most reliable approach is to process each test individually
# by finding a unique marker near t.start() for each test

print('\n\nNow applying fixes...')

# Fix 1: test_expired_lease_allows_new_claim - look for context with "old-dead-token" nearby
# Let's search for "old-dead-token" in the file
old_dead_pos = content.find('old-dead-token')
print(f'old-dead-token at position: {old_dead_pos}')

# Get context around old-dead-token
start = max(0, old_dead_pos - 200)
end = min(len(content), old_dead_pos + 50)
print('Context around old-dead-token:')
print(repr(content[start:end]))