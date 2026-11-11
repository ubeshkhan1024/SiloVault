"""Read-only baseline inspection — v2 with repo root on sys.path."""
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

# --- Settings via backend.config ---
from backend.config import settings
db_path = settings.database_path
uploads_dir = settings.uploads_dir
db_url = settings.database_url
print(f"DATABASE_PATH (settings): {db_path}")
print(f"DATABASE_URL  (settings): {db_url}")
print(f"UPLOADS_DIR   (settings): {uploads_dir}")
print(f"MAX_UPLOAD_SIZE_BYTES   : {settings.max_upload_size_bytes}")
print(f"APP_NAME/VERSION        : {settings.app_name} / {settings.app_version}")
print(f"API_V1_PREFIX           : {settings.api_v1_prefix}")
print(f"HOST/PORT               : {settings.host}:{settings.port}")

# --- DB file ---
db_file = REPO_ROOT / db_path
print(f"\nDB file exists: {db_file.exists()}  abs: {db_file.resolve()}")
if db_file.exists():
    print(f"DB file size : {db_file.stat().st_size} bytes")

# --- SQLite inspection (read-only) ---
if db_file.exists():
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT version_num FROM alembic_version")
    row = cur.fetchone()
    print(f"\nALEMBIC_REVISION: {row['version_num'] if row else 'NO alembic_version TABLE'}")

    print("---TABLES---")
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r["name"] for r in cur.fetchall()]
    for t in tables:
        print(f"  {t}")

    print("\n---ROW COUNTS---")
    for t in tables:
        cur.execute(f'SELECT COUNT(*) AS c FROM "{t}"')
        c = cur.fetchone()["c"]
        print(f"  {t}: {c}")

    if "unified_file_records" in tables:
        cur.execute("SELECT COUNT(*) AS c FROM unified_file_records WHERE provider='LOCAL'")
        print(f"  unified_file_records LOCAL: {cur.fetchone()['c']}")
        cur.execute("SELECT id, provider, name, provider_file_id, size_bytes, created_at FROM unified_file_records ORDER BY created_at DESC")
        print("\n---UNIFIED_FILE_RECORDS (all) ---")
        for r in cur.fetchall():
            print(f"  {dict(r)}")

    has_activity = "activity" in tables or any("activity" in t for t in tables)
    print(f"\nACTIVITY TABLE EXISTS: {has_activity}")

    conn.close()

# --- Uploads directory ---
up_dir = REPO_ROOT / uploads_dir
print(f"\nUPLOADS_DIR exists: {up_dir.exists()}  abs: {up_dir.resolve()}")
if up_dir.exists():
    files = [p for p in up_dir.iterdir() if p.is_file()]
    dirs = [p for p in up_dir.iterdir() if p.is_dir()]
    print(f"UPLOADS file count: {len(files)}  dir count: {len(dirs)}")
    for p in sorted(files, key=lambda x: x.name):
        print(f"  {p.name}  ({p.stat().st_size} bytes)")
    for p in sorted(dirs, key=lambda x: x.name):
        print(f"  [DIR] {p.name}")

# --- Other DB-ish files in repo root ---
print("\n---REPO-ROOT DB FILES---")
for p in sorted(REPO_ROOT.glob("*.db")):
    print(f"  {p.name}  ({p.stat().st_size} bytes)")
for p in sorted(REPO_ROOT.glob("*.bak")):
    print(f"  {p.name}  ({p.stat().st_size} bytes)")