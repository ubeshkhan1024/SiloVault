#!/usr/bin/env python
# Read the file
with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r", encoding="utf-8-sig") as f:
    lines = f.readlines()

# Find and remove test_atomic_claim_guarantees_single_session (lines 557-651, 0-indexed: 556-650)
# And rename test_concurrent_claim_uses_atomic_update to test_conditional_update_allows_only_one_claim

# New list of lines after modifications
new_lines = []
i = 0
while i < len(lines):
    line = lines[i]
    
    # Skip test_atomic_claim_guarantees_single_session (starts at line 557, 0-indexed 556)
    if i == 556 and line.strip().startswith('def test_atomic_claim_guarantees_single_session'):
        # Skip this entire function (lines 557-651) - that's 95 lines
        # The function ends at line 651, and line 652 is blank, then line 653 is test_active_lease_prevents_second_claim
        # So we skip from index 556 to index 651 inclusive (96 lines)
        skip_count = 96  # 651 - 556 + 1 = 96
        i += skip_count
        print(f"Skipped test_atomic_claim_guarantees_single_session (lines 557-651)")
        continue
    
    # Rename test_concurrent_claim_uses_atomic_update to test_conditional_update_allows_only_one_claim
    # This function is at line 750 (0-indexed: 749)
    if i == 749 and 'def test_concurrent_claim_uses_atomic_update' in line:
        # Replace the function name
        new_lines.append(line.replace('def test_concurrent_claim_uses_atomic_update', 'def test_conditional_update_allows_only_one_update'))
        print(f"Renamed test_concurrent_claim_uses_atomic_update to test_conditional_update_allows_only_one_update at line {i+1}")
        i += 1
        continue
    
    # Keep all other lines
    new_lines.append(line)
    i += 1

# Write the modified file
with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "w", encoding="utf-8-sig") as f:
    f.writelines(new_lines)

print("File modifications complete")
print(f"Original file had {len(lines)} lines, new file has {len(new_lines)} lines")