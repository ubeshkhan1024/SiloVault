import re

with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r", encoding="utf-8") as f:
    content = f.read()

# Just find t.start() and show what follows
idx = content.find('t.start()')
print('First t.start() at position:', idx)
print('Next 300 chars:')
print(repr(content[idx:idx+300]))