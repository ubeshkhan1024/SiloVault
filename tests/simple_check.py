with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r", encoding="utf-8") as f:
    content = f.read()

# Simple check: find t.start() and see what follows
idx = content.find('t.start()')
print(f"First t.start() at position: {idx}")
if idx >= 0:
    print("Next 100 chars:")
    print(repr(content[idx:idx+100]))

# Check for the claim_token is not None pattern
check = 'rec.claim_token is not None'
positions = []
idx = 0
while True:
    idx = content.find(check, idx)
    if idx == -1:
        break
    positions.append(idx)
    idx += 1
print(f"\nFound {len(positions)} occurrences of 'rec.claim_token is not None'")

# For each occurrence, show the t.start() position relative to it
for i, pos in enumerate(positions[:5]):
    # Find the t.start() before this position
    t_start = content.rfind('t.start()', 0, pos)
    print(f"\nOccurrence {i+1} at position {pos}:")
    print(f"  t.start() before it at position: {t_start}")
    if t_start >= 0:
        print(f"  Content from t.start(): {repr(content[t_start:t_start+100])}")