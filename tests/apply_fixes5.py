with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r", encoding="utf-8") as f:
    c = f.read()

# Step 1: Insert original_token after each t.start()
# t.start() positions and tokens
t_positions = [25429, 28778, 41684, 44662, 47546]
tokens = ["old-dead-token", "stale-token", "old-null-token", "old-null-token", "gen-token"]

for i, (t_pos, token) in enumerate(zip(t_positions, tokens)):
    if c[t_pos:t_pos+8] == 't.start()':
        # Insert after t.start() followed by \n\n
        # Find the position after t.start()\n\n
        search = 't.start()\n\n'
        pos = c.find(search, t_pos)
        if pos >= 0:
            # Insert the original_token line (4 spaces + original_token = "..." + \n)
            insert_line = '    original_token = "' + token + '"\n'
            c = c[:pos+len(search)] + insert_line + c[pos+len(search):]
            print(f"Fix {i+1}: Inserted original_token at position {pos}")
        else:
            print(f"Fix {i+1}: Could not find t.start()\\n\\n at position {t_pos}")

# Step 2: Replace 'rec.claim_token is not None' with 'rec.claim_token != original_token'
# This needs to be done carefully - only in the while loop context
# Let's find all occurrences and check their context

old_pattern = 'rec.claim_token is not None'
new_pattern = 'rec.claim_token != original_token'

# Find all occurrences
occurrences = []
idx = 0
while True:
    idx = c.find(old_pattern, idx)
    if idx == -1:
        break
    occurrences.append(idx)
    idx += 1

print(f"\nFound {len(occurrences)} occurrences of 'rec.claim_token is not None'")

# For each occurrence, check if it's in the while loop context
# (i.e., preceded by 'rec.status == "PROCESSING" and')
for i, pos in enumerate(occurrences):
    # Check context 50 chars before
    start = max(0, pos - 50)
    end = min(len(c), pos + 30)
    context = c[start:end]
    print(f"  Occurrence {i+1} at {pos}: context includes '{context[context.find('status'):context.find('status')+30]}' if 'status' in context else 'no status'")
    
# Step 3: Replace the pattern
for i, pos in enumerate(occurrences):
    # Replace only if in the right context
    start = max(0, pos - 50)
    end = min(len(c), pos + 30)
    context = c[start:end]
    if 'rec.status == "PROCESSING" and' in context:
        c = c.replace(old_pattern, new_pattern, 1)  # Replace only first occurrence in this context
        print(f"Replaced occurrence {i+1}")
    else:
        print(f"Skipped occurrence {i+1} - not in right context")

# Write the file
with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "w", encoding="utf-8") as f:
    f.write(c)

print("\nFile updated successfully")
print(f"Contains original_token: {'original_token' in c}")
print(f"Contains claim_token != original_token: {'claim_token != original_token' in c}")