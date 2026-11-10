"""Shared FastAPI dependencies.

Exposes reusable dependency injection helpers (such as the database session
provider) and the database initialization entrypoint. This module is
intentionally minimal so it can be imported without pulling in the full
application graph.
"""

from backend.database import Base, check_database_migrated, engine, get_db, init_db, SessionLocal

__all__ = ["Base", "engine", "SessionLocal", "get_db", "init_db", "check_database_migrated"]