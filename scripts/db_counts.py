import sqlite3, os
os.chdir(r'C:\Projects\SiloVault')
c = sqlite3.connect('silo_vault.db')
print('Tables:', [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")])
print('Files:', c.execute('SELECT COUNT(*) FROM unified_file_records').fetchone()[0])
print('Activities:', c.execute('SELECT COUNT(*) FROM activity_records').fetchone()[0])
rows = c.execute("SELECT action_type, file_name, status, timestamp FROM activity_records ORDER BY id DESC LIMIT 10").fetchall()
for row in rows:
    print(row)
