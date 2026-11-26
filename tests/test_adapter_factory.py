"""Focused tests for HVSA H1.4B AdapterFactory.

Uses an isolated in-memory SQLite session and the project's established
``memory_db_session`` pattern.  Tests verify that the ``AdapterFactory``
creates correct adapter instances from ``StorageAccount`` objects, enforces
configuration requirements, and rejects unsupported providers.

Conventions
-----------
* ``memory_db_session`` fixture creates a fresh in-memory SQLite database
  with ``Base.metadata.create_all()`` for each test.
* ``StorageAccount``, ``StorageRole``, ``StorageAccountRole`` are imported
  from ``backend.models`` and are already registered on ``Base.metadata``.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.models import StorageAccount, StorageRole, StorageAccountRole
from backend.database import Base
from backend.storage.adapter_factory import AdapterFactory
from backend.storage.errors import StorageUnsupportedError


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


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def make_account(
    session,
    provider_id: str = "LOCAL",
    display_name: str = "Test Account",
    configuration: dict | None = None,
) -> StorageAccount:
    """Create and persist a test account, returning the instance."""
    account = StorageAccount(
        provider_id=provider_id,
        display_name=display_name,
        configuration=configuration or {},
        status="pending",
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def make_role(
    session,
    name: str = "admin",
    description: str | None = None,
) -> StorageRole:
    """Create and persist a test role."""
    role = StorageRole(name=name, description=description)
    session.add(role)
    session.commit()
    session.refresh(role)
    return role


# ---------------------------------------------------------------------------
# AdapterFactory tests
# ---------------------------------------------------------------------------


def test_local_adapter_construction_with_explicit_root(memory_db_session, tmp_path):
    """LOCAL adapter is constructed with the account's explicit storage_root."""
    session = memory_db_session
    storage_root = tmp_path / "silovault"

    account = make_account(
        session,
        provider_id="LOCAL",
        display_name="Local Account",
        configuration={"storage_root": str(storage_root)},
    )

    factory = AdapterFactory(account)
    adapter = factory.create_adapter()

    from backend.storage.local import LocalAdapter
    assert isinstance(adapter, LocalAdapter)
    assert adapter.storage_root == storage_root.resolve()


def test_local_adapter_missing_storage_root(memory_db_session):
    """Missing ``storage_root`` in configuration raises ValueError."""
    session = memory_db_session

    account = make_account(
        session,
        provider_id="LOCAL",
        display_name="Local Account",
        configuration={},
    )

    factory = AdapterFactory(account)
    with pytest.raises(ValueError, match="storage_root must be a non-empty string or Path"):
        factory.create_adapter()


def test_local_adapter_empty_storage_root(memory_db_session):
    """Empty ``storage_root`` string raises ValueError."""
    session = memory_db_session

    account = make_account(
        session,
        provider_id="LOCAL",
        display_name="Local Account",
        configuration={"storage_root": ""},
    )

    factory = AdapterFactory(account)
    with pytest.raises(ValueError, match="must be a non-empty string"):
        factory.create_adapter()


def test_google_drive_adapter_without_authentication(memory_db_session):
    """Google Drive adapter is constructed without credentials or authentication."""
    session = memory_db_session

    account = make_account(
        session,
        provider_id="GOOGLE_DRIVE",
        display_name="GDrive Account",
        configuration={"folder_id": "root"},
    )

    factory = AdapterFactory(account)
    adapter = factory.create_adapter()

    from backend.storage.google_drive import GoogleDriveAdapter
    assert isinstance(adapter, GoogleDriveAdapter)
    # Credentials should be None — no authentication performed
    assert adapter._credentials is None


def test_two_locale_accounts_get_different_instances(memory_db_session, tmp_path):
    """Two LOCAL accounts must never share an adapter instance."""
    session = memory_db_session

    root1 = tmp_path / "data1"
    root2 = tmp_path / "data2"

    account1 = make_account(
        session,
        provider_id="LOCAL",
        display_name="Local Account 1",
        configuration={"storage_root": str(root1)},
    )
    account2 = make_account(
        session,
        provider_id="LOCAL",
        display_name="Local Account 2",
        configuration={"storage_root": str(root2)},
    )

    adapter1 = AdapterFactory(account1).create_adapter()
    adapter2 = AdapterFactory(account2).create_adapter()

    assert adapter1 is not adapter2
    assert adapter1.storage_root == root1.resolve()
    assert adapter2.storage_root == root2.resolve()


def test_two_google_drive_accounts_get_different_instances(memory_db_session):
    """Two GOOGLE_DRIVE accounts must never share an adapter instance."""
    session = memory_db_session

    account1 = make_account(
        session,
        provider_id="GOOGLE_DRIVE",
        display_name="GDrive Account 1",
        configuration={"folder_id": "fold1"},
    )
    account2 = make_account(
        session,
        provider_id="GOOGLE_DRIVE",
        display_name="GDrive Account 2",
        configuration={"folder_id": "fold2"},
    )

    factory1 = AdapterFactory(account1)
    factory2 = AdapterFactory(account2)

    adapter1 = factory1.create_adapter()
    adapter2 = factory2.create_adapter()

    # Different instances
    assert adapter1 is not adapter2


def test_unsupported_provider_rejection(memory_db_session):
    """Unsupported provider IDs raise ``StorageUnsupportedError``."""
    session = memory_db_session

    account = make_account(
        session,
        provider_id="ONEDRIVE",  # not yet supported
        display_name="OneDrive Account",
        configuration={},
    )

    factory = AdapterFactory(account)
    with pytest.raises(StorageUnsupportedError) as exc_info:
        factory.create_adapter()

    # The error must include the provider ID in its context
    assert exc_info.value.provider == "ONEDRIVE"


def test_invalid_account_raises_type_error(memory_db_session):
    """Passing a non-StorageAccount object raises TypeError."""
    session = memory_db_session

    # Just a plain string — not a StorageAccount
    try:
        factory = AdapterFactory("not_an_account")  # type: ignore[arg-type]
        factory.create_adapter()
        assert False, "Should have raised TypeError"
    except TypeError:
        pass  # expected


def test_invalid_configuration_raises_value_error(memory_db_session):
    """LOCAL account with non-dict configuration raises ValueError."""
    session = memory_db_session

    account = StorageAccount(
        provider_id="LOCAL",
        display_name="Local Account",
        configuration="not_a_dict",  # type: ignore[assignment]
        status="pending",
    )
    session.add(account)
    session.commit()

    factory = AdapterFactory(account)
    with pytest.raises(ValueError, match="LOCAL account configuration must be a dict"):
        factory.create_adapter()


def test_account_without_provider_id_raises_type_error(memory_db_session):
    """Account without provider_id raises TypeError on factory init."""
    account = StorageAccount(
        provider_id=None,  # type: ignore[assignment]
        display_name="Bad",
        configuration={},
        status="pending",
    )

    with pytest.raises(TypeError):
        AdapterFactory(account)


def test_local_adapter_uses_path_object(memory_db_session):
    """LOCAL adapter can be constructed with a Path object as storage_root."""
    session = memory_db_session

    account = make_account(
        session,
        provider_id="LOCAL",
        display_name="Local Account",
        configuration={"storage_root": "/tmp/silovault"},
    )

    factory = AdapterFactory(account)
    adapter = factory.create_adapter()

    from backend.storage.local import LocalAdapter
    assert isinstance(adapter, LocalAdapter)
    # Path objects are accepted by LocalAdapter.__init__
    assert adapter.storage_root is not None


def test_google_drive_adapter_credentials_none(memory_db_session):
    """Google Drive adapter has _credentials set to None after factory creation."""
    session = memory_db_session

    account = make_account(
        session,
        provider_id="GOOGLE_DRIVE",
        display_name="GDrive Account",
        configuration={},
    )

    factory = AdapterFactory(account)
    adapter = factory.create_adapter()

    from backend.storage.google_drive import GoogleDriveAdapter
    assert isinstance(adapter, GoogleDriveAdapter)
    assert adapter._credentials is None

# ---------------------------------------------------------------------------
# H1.4C: credentials_ref pass-through
# ---------------------------------------------------------------------------


def test_google_drive_adapter_receives_account_credentials_ref(memory_db_session):
    """The factory passes ``StorageAccount.credentials_ref`` to the adapter so
    credential loads/stores are account-scoped."""
    session = memory_db_session

    account = make_account(
        session,
        provider_id="GOOGLE_DRIVE",
        display_name="GDrive Account",
        configuration={},
    )
    account.credentials_ref = "acct-ref-123"
    session.commit()
    session.refresh(account)

    adapter = AdapterFactory(account).create_adapter()

    from backend.storage.google_drive import GoogleDriveAdapter
    assert isinstance(adapter, GoogleDriveAdapter)
    assert adapter._credentials_ref == "acct-ref-123"
    assert adapter._credentials is None


def test_google_drive_adapter_without_credentials_ref_keeps_legacy_behaviour(
    memory_db_session,
):
    """``credentials_ref=None`` keeps the legacy single-account entry."""
    session = memory_db_session

    account = make_account(
        session,
        provider_id="GOOGLE_DRIVE",
        display_name="Legacy GDrive Account",
        configuration={},
    )

    adapter = AdapterFactory(account).create_adapter()

    assert adapter._credentials_ref is None


def test_google_drive_accounts_do_not_share_credentials_ref(memory_db_session):
    """Two Google Drive accounts keep distinct credential references."""
    session = memory_db_session

    account1 = make_account(
        session,
        provider_id="GOOGLE_DRIVE",
        display_name="GDrive 1",
        configuration={},
    )
    account1.credentials_ref = "ref-one"

    account2 = make_account(
        session,
        provider_id="GOOGLE_DRIVE",
        display_name="GDrive 2",
        configuration={},
    )
    account2.credentials_ref = "ref-two"
    session.commit()

    adapter1 = AdapterFactory(account1).create_adapter()
    adapter2 = AdapterFactory(account2).create_adapter()

    assert adapter1 is not adapter2
    assert adapter1._credentials_ref == "ref-one"
    assert adapter2._credentials_ref == "ref-two"
