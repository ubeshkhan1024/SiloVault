"""SQLite database connection foundation.

Provides a shared SQLAlchemy engine and session factory. The database file is
defined by configuration and excluded from Git via `.gitignore`.

This is a structural skeleton. No concrete tables or models are defined yet.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.config import settings

# Create the SQLAlchemy engine.
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)

# Configure a session factory for database operations.
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """FastAPI dependency that yields a database session per request.

    The session is always closed after the request is finished, even if an
    exception is raised.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()