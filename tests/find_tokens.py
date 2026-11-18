import re

with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r", encoding="utf-8") as f:
    content = f.read()

# Search for all original tokens
tokens = ['old-dead-token', 'stale-token', 'old-null-token', 'gen-token']
for token in tokens:
    positions = []
    idx = 0
    while True:
        idx = content.find(token, idx)
        if idx == -1:
            break
        positions.append(idx)
        idx += 1
    print(f'{token}: found {len(positions)} times')
    for p in positions[:3]:  # Show first 3 positions
        start = max(0, p - 100)
        end = min(len(content), p + 50)
        print(f'  Position {p}: {repr(content[start:end])}')

# Also check the 6th occurrence of claim_token is not None - it might be in a passing test
print('\n\nAll 6 occurrences of claim_token is not None:')
check_pattern = r'if rec is not None and rec\.status == "PROCESSING" and rec\.claim_token is not None:'
check_matches = list(re.finditer(check_pattern, content))
for i, m in enumerate(check_matches):
    start = max(0, m.start() - 100)
    end = min(len(content), m.end() + 100)
    print(f'\nOccurrence {i+1} at position {m.start()}:')
    print(repr(content[start:end]))