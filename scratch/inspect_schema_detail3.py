import sqlite3
from datetime import datetime, timezone
conn = sqlite3.connect('silo_vault.db')
cursor = conn.cursor()
now = datetime.now(timezone.utc).isoformat()
# Check if NOT NULL constraint is enforced
print('=== Testing NOT NULL enforcement ===')
try:
    cursor.execute('INSERT INTO pending_deletions (id, provider, provider_account_id, provider_file_id, quarantine_id, file_name_snapshot, status, retry_count, created_at, operation_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', 
                   ('test-null-1', 'LOCAL', None, 'file-1', 'quar-1', 'test.txt', 'PENDING', 0, now, None))
    conn.commit()
    print('ERROR: NULL insert succeeded')
except sqlite3.IntegrityError as e:
    print(f'NULL correctly rejected: {e}')
print()
# Check existing rows
cursor.execute('SELECT id, operation_type, status FROM pending_deletions')
rows = cursor.fetchall()
print(f'=== Existing rows: {len(rows)} ===')
for row in rows:
    print(row)
conn.close()