#!/usr/bin/env python
import sys

with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r", encoding="utf-8") as f:
    content = f.read()

# Insert original_token after each t.start()
# We know there are 5 t.start() positions: 25429, 28778, 41684, 44662, 47546
# And the tokens are: old-dead-token, stale-token, old-null-token, old-null-token, gen-token

t_positions = [25429, 28778, 41684, 44662, 47546]
tokens = ["old-dead-token", "stale-token", "old-null-token", "old-null-token", "gen-token"]

# Insert original_token after each t.start()
for i, (t_pos, token) in enumerate(zip(t_positions, tokens)):
    # Check if we're at the right position
    if content[t_pos:t_pos+10] == 't.start()':
        # Insert after t.start()\n\n
        # The line should be:     original_token = "..." (4 spaces indent)
        new_line = '    original_token = "' + token + '"\n'
        # Insert after t.start()\n\n (t.start() is 8 chars, then \n\n is 2 chars)
        insert_pos = t_pos + 8 + 2  # after t.start()\n\n
        content = content[:insert_pos] + new_line + content[insert_pos:]
        print(f"Fix {i+1}: Inserted original_token after t.start()")
    else:
        print(f"Fix {i+1}: Wrong position {t_pos}")

# Write the modified content
with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "w", encoding="utf-8") as f:
    f.write(content)

print("\nFile updated with original_token insertions")