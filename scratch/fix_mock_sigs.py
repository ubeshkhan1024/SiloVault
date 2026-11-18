"""Fix test mock signatures using line-by-line replacement."""

path = r'c:\Projects\SiloVault\tests\test_recovery.py'
with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

fixed_finalize = False
fixed_schedule = False

for i, line in enumerate(lines):
    # Fix _tampered_finalize signature (line 422)
    if 'def _tampered_finalize(db, record, claim_token):' in line:
        lines[i] = line.replace(
            'def _tampered_finalize(db, record, claim_token):',
            'def _tampered_finalize(db, record, claim_token, claim_generation):'
        )
        fixed_finalize = True
    # Fix original_finalize call inside _tampered_finalize
    if 'return original_finalize(db, record, claim_token)' in line and not fixed_schedule:
        lines[i] = line.replace(
            'return original_finalize(db, record, claim_token)',
            'return original_finalize(db, record, claim_token, claim_generation)'
        )
    # Fix _tampered_schedule signature (line 453)
    if 'def _tampered_schedule(db, record, claim_token, error_msg,' in line:
        lines[i] = line.replace(
            'def _tampered_schedule(db, record, claim_token, error_msg,',
            'def _tampered_schedule(db, record, claim_token, claim_generation, error_msg,'
        )
        fixed_schedule = True
    # Fix original_schedule call inside _tampered_schedule
    if 'return original_schedule(db, record, claim_token, error_msg,' in line:
        lines[i] = line.replace(
            'return original_schedule(db, record, claim_token, error_msg,',
            'return original_schedule(db, record, claim_token, claim_generation, error_msg,'
        )

print(f"Fixed finalize: {fixed_finalize}, Fixed schedule: {fixed_schedule}")

with open(path, 'w', encoding='utf-8') as f:
    f.writelines(lines)

print("Signatures updated")
