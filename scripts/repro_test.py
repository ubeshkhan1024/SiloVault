"""Minimal reproduction of the test failure."""
import sys
sys.path.insert(0, r"C:\Projects\SiloVault")

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Reproduce the exact fixture pattern
test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    future=True,
)

print(f"Engine id: {id(test_engine)}")
print("Calling Base.metadata.create_all...")

from backend.database import Base
import backend.models  # ensure all models are registered

try:
    Base.metadata.create_all(bind=test_engine)
    print("SUCCESS: create_all completed")
except Exception as e:
    print(f"ERROR: {e}")
    import traceback
    traceback.print_exc()

Session = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
session = Session()
print("Session created successfully")
session.close()
test_engine.dispose()
print("Done")