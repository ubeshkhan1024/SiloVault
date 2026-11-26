"""Focused tests for HVSA H1.4A AccountManager.

Uses an isolated in-memory SQLite session for each test, following the
established ``memory_db_session`` pattern from ``tests/test_database.py``.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List
from typing import Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.models import StorageAccount, StorageRole, StorageAccountRole
from backend.database import Base
from backend.storage.account_manager import AccountManager, AccountStatus


@pytest.fixture
def memory_db_session():
    """Yields an isolated, clean SQLite session for each test."""
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(bind=test_engine)
    SessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        test_engine.dispose()

@pytest.fixture
def manager(memory_db_session):
    """Provides an AccountManager using the test's isolated session."""
    return AccountManager(memory_db_session)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def make_account(
    session,
    provider_id: str = "GOOGLE_DRIVE",
    display_name: str = "Test Account",
    credentials_ref: Optional[str] = None,
    configuration: Optional[Dict[str, Any]] = None,
) -> StorageAccount:
    """Create and persist a test account, returning the instance."""
    from backend.storage.account_manager import AccountManager

    manager = AccountManager(session)
    return manager.create_account(
        provider_id=provider_id,
        display_name=display_name,
        credentials_ref=credentials_ref,
        configuration=configuration or {},
    )


def make_role(
    session,
    name: str = "admin",
    description: Optional[str] = None,
) -> StorageRole:
    """Create and persist a test role, returning the instance."""
    from backend.models import StorageRole

    role = StorageRole(name=name, description=description)
    session.add(role)
    session.commit()
    session.refresh(role)
    return role


# ---------------------------------------------------------------------------
# AccountManager tests
# ---------------------------------------------------------------------------


def test_account_creation_and_retrieval(memory_db_session, manager):
    """An account can be created and retrieved by ID."""

    session = memory_db_session
    account = make_account(
        session,
        provider_id="LOCAL",
        display_name="Local Storage",
        configuration={"root": "/data"},
    )

    retrieved = manager.get_account(account.id)
    assert retrieved is not None
    assert retrieved.id == account.id
    assert retrieved.provider_id == "LOCAL"
    assert retrieved.display_name == "Local Storage"
    assert retrieved.credentials_ref is None
    assert retrieved.configuration == {"root": "/data"}
    assert retrieved.status == AccountStatus.PENDING


def test_multiple_accounts_same_provider(memory_db_session, manager):
    """Multiple accounts can use the same provider without limit."""
    session = memory_db_session

    # Create 3 accounts under GOOGLE_DRIVE
    accounts = [
        make_account(
            session,
            provider_id="GOOGLE_DRIVE",
            display_name=f"GDrive Account {i}",
            configuration={"folder_id": f"fold_{i}"},
        )
        for i in range(3)
    ]

    # Verify all three exist
    all_accounts = manager.list_accounts(provider_id="GOOGLE_DRIVE")
    assert len(all_accounts) == 3

    # Verify each has distinct IDs and config
    ids = {a.id for a in all_accounts}
    assert len(ids) == 3
    configs = {a.id: a.configuration for a in all_accounts}
    assert {a.display_name: a.configuration for a in all_accounts} == {f"GDrive Account {i}": {"folder_id": f"fold_{i}"} for i in range(3)}


def test_listing_all_accounts(memory_db_session, manager):
    """Listing all accounts returns every account in the session."""
    session = memory_db_session

    # Create accounts under different providers
    make_account(session, provider_id="LOCAL", display_name="Local")
    make_account(session, provider_id="GOOGLE_DRIVE", display_name="GDrive 1")
    make_account(session, provider_id="GOOGLE_DRIVE", display_name="GDrive 2")
    make_account(session, provider_id="ONEDRIVE", display_name="OneDrive")

    # List all accounts
    all_accounts = manager.list_accounts()
    assert len(all_accounts) == 4

    # List filtered by provider
    gd_accounts = manager.list_accounts(provider_id="GOOGLE_DRIVE")
    assert len(gd_accounts) == 2


def test_configuration_persistence_and_isolation(memory_db_session, manager):
    """Configuration JSON is persisted and isolated per account."""
    session = memory_db_session

    account1 = make_account(
        session,
        provider_id="BOX",
        display_name="Box Config",
        configuration={"root_folder": "/box/root", "sync_interval": 300},
    )
    account2 = make_account(
        session,
        provider_id="BOX",
        display_name="Box Config 2",
        configuration={"root_folder": "/box2/root", "sync_interval": 600},
    )

    # Verify each account has its own config
    config1 = manager.get_account_configuration(account1.id)
    config2 = manager.get_account_configuration(account2.id)

    assert config1 == {"root_folder": "/box/root", "sync_interval": 300}
    assert config2 == {"root_folder": "/box2/root", "sync_interval": 600}

    # Verify defensive copy: modifying returned dict does NOT affect DB
    config1["new_key"] = "should_not_persist"
    assert "new_key" not in manager.get_account_configuration(account1.id)


def test_update_account_details(memory_db_session, manager):
    """Account details can be updated."""
    session = memory_db_session

    account = make_account(
        session,
        provider_id="DROPBOX",
        display_name="Old Name",
        configuration={"old_key": "old_val"},
    )

    # Update display name and configuration
    updated = manager.update_account(
        account.id,
        display_name="New Name",
        configuration={"new_key": "new_val"},
    )

    assert updated.display_name == "New Name"
    assert updated.configuration == {"new_key": "new_val"}

    # Verify old values are gone
    retrieved = manager.get_account(account.id)
    assert retrieved.display_name == "New Name"
    assert retrieved.configuration == {"new_key": "new_val"}


def test_disconnect_account(memory_db_session, manager):
    """An account can be disconnected."""
    session = memory_db_session

    account = make_account(
        session,
        provider_id="GOOGLE_DRIVE",
        display_name="To Be Disconnected",
    )

    # Initially pending
    assert account.status == AccountStatus.PENDING

    # Disconnect
    disconnected = manager.disconnect_account(account.id)
    assert disconnected.status == AccountStatus.DISCONNECTED

    # Verify in DB
    retrieved = manager.get_account(account.id)
    assert retrieved.status == AccountStatus.DISCONNECTED


def test_invalid_provider_id_creation(memory_db_session, manager):
    """Creating an account with an invalid provider ID raises ValueError."""
    session = memory_db_session

    with pytest.raises(ValueError):
        manager.create_account(
            provider_id="invalid",  # lowercase, fails pattern
            display_name="Bad Provider",
        )

    with pytest.raises(ValueError):
        manager.create_account(
            provider_id="  spaced  ",  # has whitespace, fails validation
            display_name="Bad Provider",
        )

    with pytest.raises(ValueError):
        manager.create_account(
            provider_id="lowercase",  # doesn't start with uppercase letter
            display_name="Bad Provider",
        )


def test_missing_account_id_retrieval(memory_db_session, manager):
    """Retrieving a non-existent account returns None."""
    session = memory_db_session

    result = manager.get_account("00000000-0000-0000-0000-000000000000")
    assert result is None


def test_missing_account_id_update(memory_db_session, manager):
    """Updating a non-existent account raises ValueError."""
    session = memory_db_session

    with pytest.raises(ValueError):
        manager.update_account(
            "00000000-0000-0000-0000-000000000000",
            display_name="Should Not Work",
        )


def test_missing_account_id_status(memory_db_session, manager):
    """Getting status of a non-existent account raises ValueError."""
    session = memory_db_session

    with pytest.raises(ValueError):
        manager.get_account_status("00000000-0000-0000-0000-000000000000")


def test_missing_account_id_disconnect(memory_db_session, manager):
    """Disconnecting a non-existent account raises ValueError."""
    session = memory_db_session

    with pytest.raises(ValueError):
        manager.disconnect_account("00000000-0000-0000-0000-000000000000")


def test_get_account_status_returns_documented_values(memory_db_session, manager):
    """Account status uses the documented AccountStatus values."""
    session = memory_db_session

    # Create account (starts PENDING)
    account = make_account(session, provider_id="LOCAL", display_name="Test")

    # Should be PENDING initially
    status = manager.get_account_status(account.id)
    assert status == AccountStatus.PENDING

    # After disconnect, should be DISCONNECTED
    manager.disconnect_account(account.id)
    status = manager.get_account_status(account.id)
    assert status == AccountStatus.DISCONNECTED


def test_account_with_credentials_ref(memory_db_session, manager):
    """Account can be created with a credentials_ref string."""
    session = memory_db_session

    account = make_account(
        session,
        provider_id="GOOGLE_DRIVE",
        display_name="GDrive With Creds",
        credentials_ref="cref_abc123",
        configuration={"folder_id": "abc123"},
    )

    assert account.credentials_ref == "cref_abc123"
    assert account.configuration == {"folder_id": "abc123"}
    assert account.status == AccountStatus.PENDING


def test_defensive_configuration_copy(memory_db_session, manager):
    """Returned configuration is a defensive copy; mutations don't persist."""
    session = memory_db_session

    account = make_account(
        session,
        provider_id="LOCAL",
        display_name="Config Test",
        configuration={"key": "value", "nested": {"a": 1}},
    )

    # Get config and mutate the returned dict
    config = manager.get_account_configuration(account.id)
    config["key"] = "mutated"
    config["new"] = "added"
    # Deep nested mutation
    if "nested" in config and isinstance(config["nested"], dict):
        config["nested"]["a"] = 999

    # DB should be unaffected
    assert manager.get_account_configuration(account.id) == {"key": "value", "nested": {"a": 1}}
    assert "new" not in manager.get_account_configuration(account.id)


# ---------------------------------------------------------------------------
# HVSA H1.4E-A: credential-reference validation and uniqueness
# ---------------------------------------------------------------------------


def test_credentials_ref_is_trimmed_on_create(memory_db_session, manager):
    """A reference is normalized once, so whitespace variants cannot diverge."""
    account = make_account(
        memory_db_session, display_name="A", credentials_ref="  ref-a  "
    )

    assert account.credentials_ref == "ref-a"


@pytest.mark.parametrize("ref", ["", "   ", "\t\n "])
def test_empty_credentials_ref_rejected_on_create(memory_db_session, manager, ref):
    """Empty or whitespace-only references are rejected outright."""
    with pytest.raises(ValueError):
        manager.create_account(
            provider_id="GOOGLE_DRIVE", display_name="A", credentials_ref=ref
        )


@pytest.mark.parametrize("ref", ["", "   "])
def test_empty_credentials_ref_rejected_on_update(memory_db_session, manager, ref):
    """Updating an account cannot install an empty reference."""
    account = make_account(memory_db_session, display_name="A")

    with pytest.raises(ValueError):
        manager.update_account(account.id, credentials_ref=ref)

    memory_db_session.expire_all()
    assert manager.get_account(account.id).credentials_ref is None


def test_duplicate_credentials_ref_rejected_on_create(memory_db_session, manager):
    """Two accounts may not share one keyring entry."""
    make_account(memory_db_session, display_name="A", credentials_ref="ref-a")

    with pytest.raises(ValueError, match="already in use"):
        make_account(memory_db_session, display_name="B", credentials_ref="ref-a")

    assert len(manager.list_accounts()) == 1


def test_whitespace_variant_credentials_ref_rejected_on_create(
    memory_db_session, manager
):
    """A whitespace variant normalizes to the same entry and is rejected."""
    make_account(memory_db_session, display_name="A", credentials_ref="ref-a")

    with pytest.raises(ValueError, match="already in use"):
        make_account(memory_db_session, display_name="B", credentials_ref="  ref-a ")


def test_legacy_whitespace_stored_ref_still_detected(memory_db_session, manager):
    """Rows written before validation existed are still seen as collisions."""
    account = make_account(memory_db_session, display_name="A", credentials_ref="ref-a")
    account.credentials_ref = "  ref-a  "  # simulate a pre-validation row
    memory_db_session.commit()

    with pytest.raises(ValueError, match="already in use"):
        make_account(memory_db_session, display_name="B", credentials_ref="ref-a")


def test_account_can_keep_its_own_credentials_ref(memory_db_session, manager):
    """An account may re-assign (or normalize) its own reference."""
    account = make_account(memory_db_session, display_name="A", credentials_ref="ref-a")

    updated = manager.update_account(account.id, credentials_ref="ref-a")
    assert updated.credentials_ref == "ref-a"

    updated = manager.update_account(account.id, credentials_ref="  ref-a  ")
    assert updated.credentials_ref == "ref-a"


def test_update_rejects_another_accounts_credentials_ref(memory_db_session, manager):
    """Updating cannot steal another account's reference."""
    make_account(memory_db_session, display_name="A", credentials_ref="ref-a")
    account_b = make_account(
        memory_db_session, display_name="B", credentials_ref="ref-b"
    )

    with pytest.raises(ValueError, match="already in use"):
        manager.update_account(account_b.id, credentials_ref="ref-a")

    memory_db_session.expire_all()
    assert manager.get_account(account_b.id).credentials_ref == "ref-b"


def test_accounts_without_credentials_ref_are_unrestricted(memory_db_session, manager):
    """Omitting the reference keeps creation unlimited and unchanged."""
    first = make_account(memory_db_session, display_name="A")
    second = make_account(memory_db_session, display_name="B")
    third = make_account(memory_db_session, display_name="C")

    assert first.credentials_ref is None
    assert second.credentials_ref is None
    assert third.credentials_ref is None


# ---------------------------------------------------------------------------
# HVSA H1.5.2B: Account Update Cache Invalidation Tests
# ---------------------------------------------------------------------------


def test_update_account_with_new_credentials_ref_invalidates_cache(memory_db_session):
    """When credentials_ref changes, invalidate_account is called with account_id."""
    from backend.storage.account_adapter_manager import AccountAdapterManager

    account = make_account(
        memory_db_session,
        provider_id="GOOGLE_DRIVE",
        display_name="Test Account",
        credentials_ref="old-ref-123",
    )

    # Create an AccountManager for updates
    account_mgr = AccountManager(memory_db_session)
    
    # Create an AccountAdapterManager for cache
    adapter_mgr = AccountAdapterManager(memory_db_session)
    invalidate_called = []

    def mock_invalidate(account_id):
        invalidate_called.append(account_id)

    # Update with new credentials_ref
    account_mgr.update_account(account.id, credentials_ref="new-ref-456", _on_commit=mock_invalidate)

    # Verify invalidate_account was called with the correct account_id
    assert len(invalidate_called) == 1
    assert invalidate_called[0] == account.id


def test_update_account_unchanged_credentials_ref_no_invalidation(memory_db_session):
    """When credentials_ref is unchanged, _on_commit should not be invoked."""
    from backend.storage.account_adapter_manager import AccountAdapterManager

    account = make_account(
        memory_db_session,
        provider_id="GOOGLE_DRIVE",
        display_name="Test Account",
        credentials_ref="existing-ref-123",
    )

    # Create an AccountManager for updates
    account_mgr = AccountManager(memory_db_session)
    
    invalidate_called = []

    def mock_invalidate(account_id):
        invalidate_called.append(account_id)

    # Update with same credentials_ref (normalized)
    old_ref = account.credentials_ref
    account_mgr.update_account(
        account.id,
        credentials_ref=old_ref,
        _on_commit=mock_invalidate,
    )

    # Verify invalidate_account was NOT called since ref didn't change
    assert len(invalidate_called) == 0


def test_update_account_configuration_no_invalidation(memory_db_session):
    """Configuration-only updates should not trigger cache invalidation."""
    from backend.storage.account_adapter_manager import AccountAdapterManager

    account = make_account(
        memory_db_session,
        provider_id="GOOGLE_DRIVE",
        display_name="Test Account",
        credentials_ref="ref-123",
        configuration={"old_key": "old_value"},
    )

    # Create an AccountManager for updates
    account_mgr = AccountManager(memory_db_session)
    
    invalidate_called = []

    def mock_invalidate(account_id):
        invalidate_called.append(account_id)

    # Update only configuration
    account_mgr.update_account(
        account.id,
        configuration={"new_key": "new_value"},
        _on_commit=mock_invalidate,
    )

    # Verify invalidate_account was NOT called since only configuration changed
    assert len(invalidate_called) == 0


def test_update_account_commit_failure_does_not_invalidate_cache(memory_db_session):
    """When commit fails, _on_commit should not be invoked and no invalidation occurs."""
    from sqlalchemy.exc import IntegrityError
    
    account = make_account(
        memory_db_session,
        provider_id="GOOGLE_DRIVE",
        display_name="Test Account",
        credentials_ref="ref-123",
    )

    # Create an AccountManager for updates
    account_mgr = AccountManager(memory_db_session)
    
    invalidate_called = []

    def mock_invalidate(account_id):
        invalidate_called.append(account_id)

    # Simulate a commit failure by making the session raise an error on commit
    original_commit = memory_db_session.commit
    
    def failing_commit():
        raise IntegrityError("Simulated commit failure", None, None)
    
    memory_db_session.commit = failing_commit
    
    try:
        # Attempt to update - should raise IntegrityError
        with pytest.raises(IntegrityError):
            account_mgr.update_account(
                account.id,
                credentials_ref="new-ref-456",
                _on_commit=mock_invalidate,
            )
        
        # Verify callback was NOT called since commit failed
        assert len(invalidate_called) == 0, "_on_commit should not be called on commit failure"
    finally:
        # Restore original commit
        memory_db_session.commit = original_commit


def test_update_account_commit_success_refresh_failure_still_invalidates(
    memory_db_session,
):
    """When commit succeeds but refresh fails, cache invalidation still occurs.

    This test verifies:
    - _on_commit is invoked after successful commit (before refresh)
    - The refresh exception propagates
    - Cache invalidation has already happened
    """
    from sqlalchemy.exc import DBAPIError

    account = make_account(
        memory_db_session,
        provider_id="GOOGLE_DRIVE",
        display_name="Test Account",
        credentials_ref="old-ref-123",
    )

    # Create an AccountManager for updates
    account_mgr = AccountManager(memory_db_session)
    
    invalidate_called = []

    def mock_invalidate(account_id):
        invalidate_called.append(account_id)

    # Simulate a refresh failure after commit succeeds
    original_commit = memory_db_session.commit
    original_refresh = memory_db_session.refresh

    def failing_refresh(*args, **kwargs):
        raise DBAPIError("Simulated refresh failure", None, None)

    memory_db_session.commit = original_commit  # Keep commit working
    memory_db_session.refresh = failing_refresh

    try:
        # Attempt to update - should raise DBAPIError from refresh
        with pytest.raises(DBAPIError):
            account_mgr.update_account(
                account.id,
                credentials_ref="new-ref-456",
                _on_commit=mock_invalidate,
            )

        # Verify callback WAS called even though refresh failed
        assert len(invalidate_called) == 1, (
            "_on_commit should be called after successful commit even if refresh fails"
        )
        assert invalidate_called[0] == account.id
    finally:
        # Restore original methods
        memory_db_session.commit = original_commit
        memory_db_session.refresh = original_refresh


# ---------------------------------------------------------------------------
# H1.5.2B Step 2: Configuration Cache Invalidation Tests
# ---------------------------------------------------------------------------


def test_update_local_account_storage_root_changed_invalidates_cache(memory_db_session):
    """When LOCAL account's storage_root changes, invalidate_account is called."""
    account = make_account(
        memory_db_session,
        provider_id="LOCAL",
        display_name="Test Account",
        configuration={"storage_root": "/data/old"},
    )

    account_mgr = AccountManager(memory_db_session)
    invalidate_called = []

    def mock_invalidate(account_id):
        invalidate_called.append(account_id)

    # Update with new storage_root
    account_mgr.update_account(
        account.id,
        configuration={"storage_root": "/data/new"},
        _on_commit=mock_invalidate,
    )

    # Verify invalidate_account was called with the correct account_id
    assert len(invalidate_called) == 1
    assert invalidate_called[0] == account.id


def test_update_local_account_unrelated_config_no_invalidation(memory_db_session):
    """Changing unrelated LOCAL config fields should not trigger invalidation."""
    account = make_account(
        memory_db_session,
        provider_id="LOCAL",
        display_name="Test Account",
        configuration={"storage_root": "/data", "other_field": "old_value"},
    )

    account_mgr = AccountManager(memory_db_session)
    invalidate_called = []

    def mock_invalidate(account_id):
        invalidate_called.append(account_id)

    # Update only unrelated configuration field
    account_mgr.update_account(
        account.id,
        configuration={"storage_root": "/data", "other_field": "new_value"},
        _on_commit=mock_invalidate,
    )

    # Verify invalidate_account was NOT called since storage_root unchanged
    assert len(invalidate_called) == 0


def test_update_local_account_storage_root_unchanged_no_invalidation(memory_db_session):
    """When LOCAL account's storage_root is unchanged, no invalidation occurs."""
    account = make_account(
        memory_db_session,
        provider_id="LOCAL",
        display_name="Test Account",
        configuration={"storage_root": "/data/same"},
    )

    account_mgr = AccountManager(memory_db_session)
    invalidate_called = []

    def mock_invalidate(account_id):
        invalidate_called.append(account_id)

    # Update with same storage_root
    account_mgr.update_account(
        account.id,
        configuration={"storage_root": "/data/same"},
        _on_commit=mock_invalidate,
    )

    # Verify invalidate_account was NOT called since storage_root didn't change
    assert len(invalidate_called) == 0


def test_update_local_account_path_type_equivalence_no_invalidation(memory_db_session):
    """Equivalent str and Path storage_root values are not a real change.

    Configuration is JSON-persisted, so a Path object can never be stored in
    the database.  The stored value here is a normal JSON string; the
    equivalent Path representation is placed on the in-memory account only,
    and is replaced by the JSON-safe string before ``update_account`` commits.
    That exercises the comparison logic in isolation: ``Path("/data/same")``
    and ``"/data/same"`` describe the same storage_root, so the
    cache-invalidation callback must not run.
    """
    from pathlib import Path

    account = make_account(
        memory_db_session,
        provider_id="LOCAL",
        display_name="Test Account",
        configuration={"storage_root": "/data/same"},
    )

    # In-memory only: never flushed, because update_account assigns the new
    # JSON-safe configuration before committing.
    account.configuration = {"storage_root": Path("/data/same")}

    account_mgr = AccountManager(memory_db_session)
    invalidate_called = []

    def mock_invalidate(account_id):
        invalidate_called.append(account_id)

    updated = account_mgr.update_account(
        account.id,
        configuration={"storage_root": "/data/same"},
        _on_commit=mock_invalidate,
    )

    # Verify invalidate_account was NOT called: the Path and str are equivalent
    assert len(invalidate_called) == 0
    # And the persisted configuration is still plain JSON (a string, not a Path)
    assert updated.configuration == {"storage_root": "/data/same"}
    assert isinstance(updated.configuration["storage_root"], str)


def test_update_google_drive_account_config_no_invalidation(memory_db_session):
    """Google Drive configuration-only updates should not trigger invalidation."""
    account = make_account(
        memory_db_session,
        provider_id="GOOGLE_DRIVE",
        display_name="Test Account",
        credentials_ref="ref-123",
        configuration={"some_field": "old_value"},
    )

    account_mgr = AccountManager(memory_db_session)
    invalidate_called = []

    def mock_invalidate(account_id):
        invalidate_called.append(account_id)

    # Update only configuration (Google Drive doesn't use configuration)
    account_mgr.update_account(
        account.id,
        configuration={"some_field": "new_value"},
        _on_commit=mock_invalidate,
    )

    # Verify invalidate_account was NOT called since Google Drive doesn't use config
    assert len(invalidate_called) == 0


def test_update_local_account_commit_failure_storage_root_change(memory_db_session):
    """When commit fails, _on_commit should not be invoked even with storage_root change."""
    from sqlalchemy.exc import IntegrityError

    account = make_account(
        memory_db_session,
        provider_id="LOCAL",
        display_name="Test Account",
        configuration={"storage_root": "/data/old"},
    )

    account_mgr = AccountManager(memory_db_session)
    invalidate_called = []

    def mock_invalidate(account_id):
        invalidate_called.append(account_id)

    # Simulate a commit failure
    original_commit = memory_db_session.commit

    def failing_commit():
        raise IntegrityError("Simulated commit failure", None, None)

    memory_db_session.commit = failing_commit

    try:
        # Attempt to update - should raise IntegrityError
        with pytest.raises(IntegrityError):
            account_mgr.update_account(
                account.id,
                configuration={"storage_root": "/data/new"},
                _on_commit=mock_invalidate,
            )

        # Verify callback was NOT called since commit failed
        assert len(invalidate_called) == 0, "_on_commit should not be called on commit failure"
    finally:
        # Restore original commit
        memory_db_session.commit = original_commit


def test_update_local_account_commit_success_refresh_failure_storage_root_change(
    memory_db_session,
):
    """When LOCAL account storage_root changes, commit succeeds but refresh fails."""
    from sqlalchemy.exc import DBAPIError

    account = make_account(
        memory_db_session,
        provider_id="LOCAL",
        display_name="Test Account",
        configuration={"storage_root": "/data/old"},
    )

    account_mgr = AccountManager(memory_db_session)
    invalidate_called = []

    def mock_invalidate(account_id):
        invalidate_called.append(account_id)

    # Simulate a refresh failure after commit succeeds
    original_commit = memory_db_session.commit
    original_refresh = memory_db_session.refresh

    def failing_refresh(*args, **kwargs):
        raise DBAPIError("Simulated refresh failure", None, None)

    memory_db_session.commit = original_commit  # Keep commit working
    memory_db_session.refresh = failing_refresh

    try:
        # Attempt to update - should raise DBAPIError from refresh
        with pytest.raises(DBAPIError):
            account_mgr.update_account(
                account.id,
                configuration={"storage_root": "/data/new"},
                _on_commit=mock_invalidate,
            )

        # Verify callback WAS called even though refresh failed
        assert len(invalidate_called) == 1, (
            "_on_commit should be called after successful commit even if refresh fails"
        )
        assert invalidate_called[0] == account.id
    finally:
        # Restore original methods
        memory_db_session.commit = original_commit
        memory_db_session.refresh = original_refresh


