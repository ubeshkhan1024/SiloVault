import sqlite3
conn = sqlite3.connect('silo_vault.db')
cursor = conn.cursor()
# Check the actual table definition from sqlite_master
cursor.execute('SELECT sql FROM sqlite_master WHERE type="table" AND name="pending_deletions"')
print('=== Table Definition ===')
print(cursor.fetchone()[0])
print()
# Check pragma table_info
cursor.execute('PRAGMA table_info(pending_deletions)')
print('=== PRAGMA table_info ===')
for row in cursor.fetchall():
    if row[1] == 'operation_type':
        print(f'cid={row[0]}, name={row[1]}, type={row[2]}, notnull={row[3]}, dflt_value={row[4]}, pk={row[5]}')
print()
# Check if NOT NULL constraint is enforced
print('=== Testing NOT NULL enforcement ===')
try:
    cursor.execute('INSERT INTO pending_deletions (id, provider, provider_account_id, provider_file_id, quarantine_id, file_name_snapshot, status, retry_count, operation_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', 
                   ('test-null-1', 'LOCAL', None, 'file-1', 'quar-1', 'test.txt', 'PENDING', 0, None))
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