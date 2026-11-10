"""Alembic environment configuration for SiloVault.

Reads the application's pydantic-settings so that Alembic always operates on
the same database file that the FastAPI app uses — no second hardcoded path.
Also imports every registered SQLAlchemy model so autogenerate sees the full
current schema, including composite and partial indexes.
"""

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# ---------------------------------------------------------------------------
# Ensure the repo root is on sys.path so ``backend`` imports resolve correctly
# when Alembic is invoked from the project root (the default working directory).
# ---------------------------------------------------------------------------
this_file = os.path.abspath(__file__)
repo_root = os.path.dirname(os.path.dirname(this_file))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

# ---------------------------------------------------------------------------
# Load application settings — these drive the real database URL.
# ---------------------------------------------------------------------------
from backend.config import settings  # noqa: E402

# ---------------------------------------------------------------------------
# Alembic Config object.
# ---------------------------------------------------------------------------
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# Target metadata: import the shared Base and ALL registered models so that
# autogenerate sees every table, index, constraint, and custom type.
# ---------------------------------------------------------------------------
from backend.database import Base  # noqa: E402
import backend.models  # noqa: E402  # side-effect: registers UnifiedFileRecord on Base.metadata
target_metadata = Base.metadata


def _resolve_database_url() -> str:
    """Return the database URL Alembic should operate on.

    Uses ``settings.database_url`` so that Alembic always targets the same
    database file as the FastAPI application — there is no second, hardcoded
    path. The ``DATABASE_PATH`` environment variable (already honoured by
    ``pydantic-settings``) lets a temporary database be selected for
    autogenerate or testing without editing any configuration file:

        DATABASE_PATH=alembic_temp.db alembic revision --autogenerate -m "..."
    """
    return settings.database_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just the URL and not an Engine.
    """
    url = _resolve_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # required for SQLite constraint/index changes
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Creates an engine with the same WAL/timeout pragmas the application uses,
    so migration behaviour matches production as closely as possible.
    """
    from sqlalchemy import create_engine, event  # noqa: I001

    url = _resolve_database_url()
    connectable = create_engine(
        url,
        connect_args={"check_same_thread": False},
        poolclass=pool.NullPool,
    )

    # Mirror the same SQLite pragmas used by the application engine.
    @event.listens_for(connectable, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ARG001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA busy_timeout=5000;")
        finally:
            cursor.close()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # required for SQLite unique/constraint changes
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
