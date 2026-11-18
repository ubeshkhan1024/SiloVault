"""Test migration on temporary database."""
import tempfile
import os
import logging
from sqlalchemy import create_engine, text
from alembic.config import Config
from alembic import command

# Enable logging to see backfill messages
logging.basicConfig(level=logging.INFO)

# Create a temporary database
with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
    db_path = f.name

try:
    # Create engine and run migrations up to the head before our new one
    engine = create_engine(f'sqlite:///{db_path}')
    
    # Run alembic migrations
    alembic_cfg = Config('alembic.ini')
    alembic_cfg.set_main_option('sqlalchemy.url', f'sqlite:///{db_path}')
    
    # Upgrade to the head before our new migration
    command.upgrade(alembic_cfg, '20261116185600')
    print('Upgraded to 20261116185600')
    
    # Verify the schema before our migration
    with engine.connect() as conn:
        result = conn.execute(text('PRAGMA table_info(pending_deletions)'))
        cols = result.fetchall()
        for col in cols:
            if col[1] == 'operation_type':
                print(f'operation_type before: nullable={col[3]}, default={col[4]}')
    
    # Now upgrade to our new migration
    command.upgrade(alembic_cfg, 'head')
    print('Upgraded to head (20261117221800)')
    
    # Verify the schema after our migration
    with engine.connect() as conn:
        result = conn.execute(text('PRAGMA table_info(pending_deletions)'))
        cols = result.fetchall()
        for col in cols:
            if col[1] == 'operation_type':
                print(f'operation_type after: nullable={col[3]}, default={col[4]}')
        
        # Check constraint
        result = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='index' AND name='ck_pending_deletions_operation_type'"))
        print(f'Constraint: {result.fetchone()}')
        
        # Test inserting NULL (should fail)
        try:
            conn.execute(text('INSERT INTO pending_deletions (id, provider, provider_account_id, provider_file_id, quarantine_id, file_name_snapshot, status, operation_type) VALUES (:id, :provider, :acc, :pfid, :qid, :fname, :status, :optype)'), 
                        {'id': 'test-1', 'provider': 'LOCAL', 'acc': None, 'pfid': 'file-1', 'qid': 'quar-1', 'fname': 'test.txt', 'status': 'PENDING', 'optype': None})
            conn.commit()
            print('ERROR: NULL insert succeeded (should have failed)')
        except Exception as e:
            print(f'NULL insert correctly rejected: {type(e).__name__}')
        
        # Test inserting CLEANUP (should succeed)
        conn.execute(text('INSERT INTO pending_deletions (id, provider, provider_account_id, provider_file_id, quarantine_id, file_name_snapshot, status, operation_type) VALUES (:id, :provider, :acc, :pfid, :qid, :fname, :status, :optype)'), 
                    {'id': 'test-2', 'provider': 'LOCAL', 'acc': None, 'pfid': 'file-2', 'qid': 'quar-2', 'fname': 'test.txt', 'status': 'PENDING', 'optype': 'CLEANUP'})
        conn.commit()
        print('CLEANUP insert succeeded')
        
        # Test inserting RESTORE (should succeed)
        conn.execute(text('INSERT INTO pending_deletions (id, provider, provider_account_id, provider_file_id, quarantine_id, file_name_snapshot, status, operation_type) VALUES (:id, :provider, :acc, :pfid, :qid, :fname, :status, :optype)'), 
                    {'id': 'test-3', 'provider': 'LOCAL', 'acc': None, 'pfid': 'file-3', 'qid': 'quar-3', 'fname': 'test.txt', 'status': 'PENDING', 'optype': 'RESTORE'})
        conn.commit()
        print('RESTORE insert succeeded')
        
        # Test inserting invalid value (should fail)
        try:
            conn.execute(text('INSERT INTO pending_deletions (id, provider, provider_account_id, provider_file_id, quarantine_id, file_name_snapshot, status, operation_type) VALUES (:id, :provider, :acc, :pfid, :qid, :fname, :status, :optype)'), 
                        {'id': 'test-4', 'provider': 'LOCAL', 'acc': None, 'pfid': 'file-4', 'qid': 'quar-4', 'fname': 'test.txt', 'status': 'PENDING', 'optype': 'INVALID'})
            conn.commit()
            print('ERROR: INVALID insert succeeded (should have failed)')
        except Exception as e:
            print(f'INVALID insert correctly rejected: {type(e).__name__}')
        
        # Check backfilled rows
        result = conn.execute(text('SELECT id, operation_type FROM pending_deletions'))
        for row in result.fetchall():
            print(f'Row: {row}')
            
finally:
    os.unlink(db_path)