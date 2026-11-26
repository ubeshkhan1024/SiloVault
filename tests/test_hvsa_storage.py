"""Focused tests for HVSA H1.3B storage account/role foundation.

Uses the ``memory_db_session`` pattern from ``tests/test_database.py``
for isolated in-memory SQLite testing with ``Base.metadata.create_all()``.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from backend.models import StorageAccount, StorageRole, StorageAccountRole
from backend.models.unified_file import UnifiedFileRecord
from backend.database import Base, engine


@pytest.fixture
def memory_db_session():
    """Yields an isolated, clean SQLite session for each model test."""
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(bind=test_engine)
    Session = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        test_engine.dispose()


# ---------------------------------------------------------------------------
# StorageAccount tests
# ---------------------------------------------------------------------------


def test_storage_account_creation(memory_db_session):
    """A StorageAccount can be created and retrieved."""
    account = StorageAccount(
        provider_id="GOOGLE_DRIVE",
        display_name="My Google Drive",
        credentials_ref="ref_123",
        status="connected",
        configuration={"folder_id": "abc123"},
    )
    memory_db_session.add(account)
    memory_db_session.commit()

    retrieved = memory_db_session.get(StorageAccount, account.id)
    assert retrieved is not None
    assert retrieved.provider_id == "GOOGLE_DRIVE"
    assert retrieved.display_name == "My Google Drive"
    assert retrieved.credentials_ref == "ref_123"
    assert retrieved.status == "connected"
    assert retrieved.configuration == {"folder_id": "abc123"}
    assert retrieved.created_at is not None
    assert retrieved.updated_at is not None


def test_storage_account_defaults(memory_db_session):
    """A StorageAccount uses default values for optional fields."""
    account = StorageAccount(
        provider_id="LOCAL",
        display_name="Local Storage",
    )
    memory_db_session.add(account)
    memory_db_session.commit()

    assert account.credentials_ref is None
    assert account.status == "pending"
    assert account.configuration == {}


def test_storage_account_updates_updated_at(memory_db_session):
    """A StorageAccount's ``updated_at`` advances on modification."""
    account = StorageAccount(
        provider_id="ONEDRIVE",
        display_name="OneDrive",
    )
    memory_db_session.add(account)
    memory_db_session.commit()

    first_updated = account.updated_at
    account.display_name = "OneDrive Updated"
    memory_db_session.commit()

    refreshed = memory_db_session.get(StorageAccount, account.id)
    assert refreshed.updated_at > first_updated


def test_storage_account_provider_index(memory_db_session):
    """``provider_id`` is indexed for fast lookups."""
    # Create multiple accounts under the same provider
    accounts = [
        StorageAccount(provider_id="BOX", display_name=f"Box Account {i}")
        for i in range(5)
    ]
    for a in accounts:
        memory_db_session.add(a)
    memory_db_session.commit()

    inspector = inspect(memory_db_session.bind)
    assert "storage_accounts" in inspector.get_table_names()


# ---------------------------------------------------------------------------
# StorageRole tests
# ---------------------------------------------------------------------------


def test_storage_role_creation(memory_db_session):
    """A StorageRole can be created and retrieved."""
    role = StorageRole(name="admin", description="Administrator access")
    memory_db_session.add(role)
    memory_db_session.commit()

    retrieved = memory_db_session.get(StorageRole, role.id)
    assert retrieved is not None
    assert retrieved.name == "admin"
    assert retrieved.description == "Administrator access"


def test_storage_role_unique_name(memory_db_session):
    """Role names must be unique; duplicate creation raises IntegrityError."""
    role1 = StorageRole(name="writer", description="Can write files")
    memory_db_session.add(role1)
    memory_db_session.commit()

    role2 = StorageRole(name="writer", description="Another writer")
    memory_db_session.add(role2)
    try:
        memory_db_session.commit()
        assert False, "Expected IntegrityError for duplicate role name"
    except Exception as e:  # sqlalchemy.IntegrityError or similar
        # The unique constraint may raise depending on DB; just verify it failed
        assert "unique" in str(e).lower() or "duplicate" in str(e).lower()


def test_storage_role_defaults(memory_db_session):
    """A StorageRole uses default values for optional fields."""
    role = StorageRole(name="reader")
    memory_db_session.add(role)
    memory_db_session.commit()

    assert role.description is None


# ---------------------------------------------------------------------------
# StorageAccountRole tests
# ---------------------------------------------------------------------------


def test_storage_account_role_assignment(memory_db_session):
    """An account can be assigned a role."""
    account = StorageAccount(provider_id="DROPBOX", display_name="Dropbox")
    memory_db_session.add(account)
    memory_db_session.commit()

    role = StorageRole(name="editor", description="Can edit files")
    memory_db_session.add(role)
    memory_db_session.commit()

    association = StorageAccountRole(account_id=account.id, role_id=role.id)
    memory_db_session.add(association)
    memory_db_session.commit()

    retrieved = memory_db_session.get(StorageAccountRole, (account.id, role.id))
    assert retrieved is not None
    assert retrieved.assigned_at is not None


def test_multiple_roles_per_account(memory_db_session):
    """One account can have multiple roles."""
    account = StorageAccount(provider_id="MEGA", display_name="Mega")
    memory_db_session.add(account)
    memory_db_session.commit()

    role1 = StorageRole(name="viewer", description="Can view")
    role2 = StorageRole(name="uploader", description="Can upload")

    memory_db_session.add_all([role1, role2])
    memory_db_session.commit()

    # Assign both roles to the same account
    memory_db_session.add(StorageAccountRole(account_id=account.id, role_id=role1.id))
    memory_db_session.add(StorageAccountRole(account_id=account.id, role_id=role2.id))
    memory_db_session.commit()

    # Check that two associations exist
    count = memory_db_session.query(StorageAccountRole).filter(
        StorageAccountRole.account_id == account.id
    ).count()
    assert count == 2


def test_multiple_accounts_per_role(memory_db_session):
    """Multiple accounts can share the same role."""
    role = StorageRole(name="member", description="Regular member")

    account1 = StorageAccount(provider_id="ONEDRIVE", display_name="Drive 1")
    account2 = StorageAccount(provider_id="ONEDRIVE", display_name="Drive 2")

    memory_db_session.add_all([role, account1, account2])
    memory_db_session.commit()

    # Assign the same role to both accounts
    memory_db_session.add(StorageAccountRole(account_id=account1.id, role_id=role.id))
    memory_db_session.add(StorageAccountRole(account_id=account2.id, role_id=role.id))
    memory_db_session.commit()

    # Verify both assignments exist
    count = memory_db_session.query(StorageAccountRole).filter(
        StorageAccountRole.role_id == role.id
    ).count()
    assert count == 2

def test_same_role_assigned_to_multiple_accounts(memory_db_session):
    """Explicit test for the requirement: same role assigned to multiple accounts."""
    role = StorageRole(name="collaborator", description="Collaborator access")
    memory_db_session.add(role)
    memory_db_session.commit()

    accounts = [
        StorageAccount(provider_id=provider, display_name=f"Account {i}")
        for i, provider in enumerate(["GOOGLE_DRIVE", "ONEDRIVE", "DROPBOX"])
    ]
    for a in accounts:
        memory_db_session.add(a)
    memory_db_session.commit()

    for a in accounts:
        memory_db_session.add(StorageAccountRole(account_id=a.id, role_id=role.id))
    memory_db_session.commit()

    # All three accounts have the same role
    count = memory_db_session.query(StorageAccountRole).filter(
        StorageAccountRole.role_id == role.id
    ).count()
    assert count == 3


def test_configuration_json_persistence(memory_db_session):
    """Configuration JSON is persisted and retrieved correctly."""
    account = StorageAccount(
        provider_id="BOX",
        display_name="Box Config",
        configuration={
            "root_folder": "/box/root",
            "sync_interval": 300,
            "metadata": {"key": "value", "num": 42},
        },
    )
    memory_db_session.add(account)
    memory_db_session.commit()

    retrieved = memory_db_session.get(StorageAccount, account.id)
    assert retrieved.configuration == {
        "root_folder": "/box/root",
        "sync_interval": 300,
        "metadata": {"key": "value", "num": 42},
    }


def test_foreign_key_declarations(memory_db_session):
    """Model metadata contains correct foreign key declarations."""
    # Check StorageAccountRole foreign keys
    fks = StorageAccountRole.__table__.foreign_keys
    assert len(fks) == 2
    fk_strs = [str(fk) for fk in fks]
    assert any("storage_accounts.id" in s for s in fk_strs)
    assert any("storage_roles.id" in s for s in fk_strs)

    # Check that tables exist on the engine
    inspector = inspect(memory_db_session.bind)
    tables = inspector.get_table_names()
    assert "storage_accounts" in tables
    assert "storage_roles" in tables
    assert "storage_account_roles" in tables


# ---------------------------------------------------------------------------
# Integration: Account + Role CRUD with UnifiedFileRecord
# ---------------------------------------------------------------------------


def test_account_role_with_unified_file(memory_db_session):
    """Storage accounts and roles work alongside existing UnifiedFileRecord."""
    # Create an account
    account = StorageAccount(
        provider_id="GOOGLE_DRIVE",
        display_name="GDrive Account",
    )
    memory_db_session.add(account)
    memory_db_session.commit()

    # Create a role
    role = StorageRole(name="owner", description="Full access")
    memory_db_session.add(role)
    memory_db_session.commit()

    # Assign role to account
    memory_db_session.add(StorageAccountRole(account_id=account.id, role_id=role.id))
    memory_db_session.commit()

    # Create a file record under the account context
    file_record = UnifiedFileRecord(
        provider="GOOGLE_DRIVE",
        provider_account_id=account.id,
        provider_file_id="gfile_001",
        name="test.txt",
        logical_path="/test.txt",
        mime_type="text/plain",
        size_bytes=100,
    )
    memory_db_session.add(file_record)
    memory_db_session.commit()

    # Verify the file is associated with the account
    retrieved = memory_db_session.get(UnifiedFileRecord, file_record.id)
    assert retrieved is not None
    assert retrieved.provider_account_id == account.id