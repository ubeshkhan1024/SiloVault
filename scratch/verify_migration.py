import sqlite3, sys
sys.path.insert(0, r'C:\Projects\SiloVault')
c = sqlite3.connect(r'C:\Projects\SiloVault\scratch\basetest\silo_vault.db')
tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print('Tables:', tables)
if 'unified_file_records' in tables:
    print('Files count:', c.execute('SELECT COUNT(*) FROM unified_file_records').fetchone()[0])
else:
    print('ERROR: unified_file_records table missing!')
if 'activity_records' in tables:
    print('Activities count:', c.execute('SELECT COUNT(*) FROM activity_records').fetchone()[0])
else:
    print('ERROR: activity_records table missing!')
