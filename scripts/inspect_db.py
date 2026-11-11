import os
from sqlalchemy import create_engine, text

os.chdir(r"C:\Projects\SiloVault")
e = create_engine("sqlite:///silo_vault.db")

with e.connect() as conn:
    print("---TABLES---")
    for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")):
        print(r[0])

    print("---CURRENT REVISION---")
    for r in conn.execute(text("SELECT version_num FROM alembic_version")):
        print(r[0])

    print("---FILE RECORDS---")
    try:
        for r in conn.execute(text("SELECT id, name, provider, status FROM unified_file_records")):
            print(r)
    except Exception as ex:
        print(f"Error reading files: {ex}")

    print("---ACTIVITY RECORDS COUNT---")
    try:
        for r in conn.execute(text("SELECT COUNT(*) FROM activity_records")):
            print(r[0])
    except Exception as ex:
        print(f"Error reading activity: {ex}")

print("---UPLOADS DIR---")
updir = "backend/uploads"
if os.path.isdir(updir):
    count = 0
    for f in sorted(os.listdir(updir)):
        fp = os.path.join(updir, f)
        if os.path.isfile(fp):
            count += 1
            print(f"{f} ({os.path.getsize(fp)} bytes)")
    print(f"(total files: {count})")
else:
    print("(uploads dir not found)")