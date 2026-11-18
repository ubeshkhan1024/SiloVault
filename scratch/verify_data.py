from backend.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()
try:
    # Check existing data
    result = db.execute(text('SELECT id, operation_type, status FROM pending_deletions'))
    rows = result.fetchall()
    print(f'Total rows: {len(rows)}')
    for row in rows:
        print(f'Row: {row}')
finally:
    db.close()