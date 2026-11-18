import re

with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r", encoding="utf-8") as f:
    content = f.read()

# Find all t.start() positions
positions = []
idx = 0
while True:
    idx = content.find('t.start()', idx)
    if idx == -1:
        break
    positions.append(idx)
    idx += 1
    
print(f'Found {len(positions)} occurrences of t.start()')
for i, pos in enumerate(positions):
    print(f'\nOccurrence {i+1} at position {pos}:')
    print(repr(content[pos:pos+200]))