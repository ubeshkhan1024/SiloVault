"""Test that application startup gates on Alembic migration status.

Verifies that the app never silently creates or modifies tables at runtime;
schema ownership belongs exclusively to Alembic.
"""

import pytest


def test_check_database_migrated_passes_when_stamped(tmp_path, monkeypatch):
    """When alembic_version exists, check_database_migrated must succeed."""
    import sqlite3
    db_path = str(tmp_path / "migrated.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"
    )
    conn.execute("INSERT INTO alembic_version VALUES ('b436c12118f9')")
    conn.commit()
    conn.close()

    from backend.database import check_database_migrated
    # Must not raise when the version table exists.
    check_database_migrated()


def test_check_database_migrated_raises_on_unmigrated_db(tmp_path, monkeypatch):
    """When alembic_version is absent, check_database_migrated must raise."""
    from backend.config import settings
    from backend.database import engine, check_database_migrated
    import sqlalchemy as sa

    test_engine = sa.create_engine(
        f"sqlite:///{tmp_path}/unmigrated.db",
        connect_args={"check_same_thread": False},
        future=True,
    )
    try:
        # Temporarily swap the production engine so the check hits our temp DB.
        original = engine
        from backend import database as db_module
        db_module.engine = test_engine

        with pytest.raises(RuntimeError) as exc_info:
            check_database_migrated()

        assert "alembic upgrade head" in str(exc_info.value)
        assert "Alembic migrations" in str(exc_info.value)
    finally:
        db_module.engine = original
        test_engine.dispose()


def test_main_lifespan_calls_check_not_create_all():
    """Application startup must call check_database_migrated and never
    invoke Base.metadata.create_all() at runtime."""
    import ast, pathlib

    src = pathlib.Path("backend/main.py").read_text(encoding="utf-8")

    # The lifespan body must reference check_database_migrated.
    assert "check_database_migrated" in src, (
        "lifespan must call check_database_migrated"
    )
    # No call to create_all or init_db may appear anywhere in main.py.
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in ("create_all", "init_db"):
                pytest.fail(
                    f"main.py must not call {name}() — schema changes are "
                    "handled exclusively by Alembic migrations."
                )
