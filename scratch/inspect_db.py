import sqlite3
conn = sqlite3.connect('silo_vault.db')
cursor = conn.cursor()
cursor.execute('PRAGMA table_info(pending_deletions)')
for row in cursor.fetchall():
    print(row)
print('---')
cursor.execute('SELECT sql FROM sqlite_master WHERE type="table" AND name="pending_deletions"')
print(cursor.fetchone()[0])
print('---')
cursor.execute('SELECT id, provider, status, operation_type, operation_generation FROM pending_deletions')
for row in cursor.fetchall():
    print(row)