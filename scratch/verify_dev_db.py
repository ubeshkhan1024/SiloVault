import sys
sys.path.insert(0, 'c:\\Projects\\SiloVault')
from backend.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()
try:
    result = db.execute(text('PRAGMA table_info(pending_deletions)'))
    cols = result.fetchall()
    for col in cols:
        if col[1] == 'operation_type':
            print(f'operation_type: nullable={col[3]}, default={col[4]}')
    
    result = db.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='pending_deletions'"))
    print(f'Table schema: {result.fetchone()[0]}')
    
    result = db.execute(text('SELECT id, operation_type, status FROM pending_deletions'))
    for row in result.fetchall():
        print(f'Row: {row}')
finally:
    db.close()