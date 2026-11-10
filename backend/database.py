"""SQLite database connection foundation.

Provides a shared SQLAlchemy engine, a declarative base, and a session
factory for the FastAPI application.

Schema ownership
----------------
**Alembic is the authoritative schema-versioning mechanism.** The
application never creates or alters tables on its own: schema changes are
made exclusively through reviewed migration scripts (see
``alembic/versions/``) applied with ``alembic upgrade head``.

A fresh installation must run ``alembic upgrade head`` *before* starting
the application. On startup, :func:`check_database_migrated` verifies that
the ``alembic_version`` table exists and raises a clear, actionable error
if the database has not been migrated yet — it never silently creates
tables outside migration history.

The database file path is defined by configuration (``DATABASE_PATH``) and
excluded from Git via ``.gitignore``.

Model registration contract
----------------------------
All SQLAlchemy model modules must be imported before ``Base.metadata`` is
consumed by Alembic's ``env.py``; otherwise their tables are not registered
on ``Base.metadata`` and autogenerate will miss them.

``backend.models`` is the single package that re-exports every concrete
model. When adding a new model module, it must be imported from
:mod:`backend.models.__init__` so that the import chain reaches it.

This module must not import :mod:`backend.models` at module load time,
because models import ``Base`` from here.
"""

import logging

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from backend.config import settings


class Base(DeclarativeBase):
    """Shared declarative base for all SiloVault ORM models.

    Concrete models are defined under :mod:`backend.models` and inherit from
    this base so that ``Base.metadata`` knows about every table. Keeping the
    base here (rather than in a dedicated module) avoids an import cycle: the
    models package imports from this module, and this module must not import
    the models package at module load time.
    """


# Create the SQLAlchemy engine. SQLite is a file-backed, local-only database,
# so it is never exposed to the network. ``check_same_thread=False`` allows
# sessions to be used across threads (required by FastAPI/uvicorn's threaded
# worker model) at the cost of relying on SQLite's own locking for safety.
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    echo=False,
    future=True,
)

# Enable WAL mode and a busy timeout so concurrent local access degrades
# gracefully instead of raising ``database is locked`` errors immediately.
@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):  # pragma: no cover
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA busy_timeout=5000;")
    finally:
        cursor.close()


# Configure a session factory for database operations. ``autocommit=False``
# and ``autoflush=False`` give explicit control over when changes are
# flushed/committed, which matters for the reversible-operation guarantees
# required by SiloVault's safety rules.
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    future=True,
)


def check_database_migrated() -> None:
    """Verify that the database has been migrated by Alembic.

    This is the application's startup gate. It does **not** create or alter
    any tables — schema ownership belongs to Alembic. It only confirms that
    the ``alembic_version`` table (created by the first ``alembic upgrade``)
    exists and contains at least one revision.

    Raises
    ------
    RuntimeError
        If the database has not been migrated yet. The message tells the
        operator to run ``alembic upgrade head`` before starting the app.

    A brand-new empty SQLite file (no tables at all) is the expected state
    before the first migration; the check raises so the operator knows to
    run the migration. An existing, migrated database passes silently.
    """
    with engine.connect() as conn:
        inspector = inspect(conn)
        tables = inspector.get_table_names()

    if "alembic_version" not in tables:
        raise RuntimeError(
            "SiloVault database has not been migrated.\n"
            "Run the following command from the repository root before "
            "starting the application:\n\n"
            "    alembic upgrade head\n\n"
            "The application never creates schema on its own; all schema "
            "changes are applied through reviewed Alembic migrations."
        )


def init_db() -> None:
    """Create all tables registered on ``Base.metadata``.

    .. deprecated::
        Kept for backward compatibility with existing tests and scripts.
        For application startup, use :func:`check_database_migrated` instead,
        which verifies Alembic-migration status without creating or altering
        tables outside migration history.
    """
    import backend.models  # noqa: F401  # registers models on Base.metadata
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency that yields a database session per request.

    The session is always closed after the request is finished, even if an
    exception is raised. The ``try/finally`` guarantee ensures no leaked
    sessions accumulate under error conditions.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()