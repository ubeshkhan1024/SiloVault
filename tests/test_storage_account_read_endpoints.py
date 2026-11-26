"""Focused tests for the HVSA H1.5.3 read-only storage account endpoints.

GET /api/v1/storage-accounts
GET /api/v1/storage-accounts/{account_id}

Each test uses an isolated in-memory SQLite database wired into the app through
``dependency_overrides`` — the real application database is never touched.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.v1.endpoints.storage_accounts import get_storage_account
from backend.database import Base, get_db
from backend.main import app
from backend.models import StorageAccount
from backend.storage.account_manager import AccountManager

BASE_URL = "/api/v1/storage-accounts"

RESPONSE_KEYS = {
    "id",
    "provider_id",
    "display_name",
    "status",
    "configuration",
    "created_at",
    "updated_at",
}


@pytest.fixture
def env():
    """TestClient wired to an isolated in-memory DB."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    session = TestSession()

    try:
        yield SimpleNamespace(
            client=TestClient(app, raise_server_exceptions=False),
            session=session,
            engine=engine,
        )
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


def make_account(
    session,
    *,
    provider_id="GOOGLE_DRIVE",
    display_name="Test Account",
    credentials_ref=None,
    configuration=None,
) -> StorageAccount:
    return AccountManager(session).create_account(
        provider_id=provider_id,
        display_name=display_name,
        credentials_ref=credentials_ref,
        configuration=configuration or {},
    )


# ---------------------------------------------------------------------------
# Collection endpoint
# ---------------------------------------------------------------------------


def test_list_all_accounts(env):
    first = make_account(env.session, provider_id="LOCAL", display_name="Local A")
    second = make_account(
        env.session,
        provider_id="GOOGLE_DRIVE",
        display_name="Drive B",
        credentials_ref="ref-b",
    )

    response = env.client.get(BASE_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert {item["id"] for item in body["items"]} == {first.id, second.id}
    assert all(set(item) == RESPONSE_KEYS for item in body["items"])


def test_list_filters_by_provider(env):
    make_account(env.session, provider_id="LOCAL", display_name="Local A")
    make_account(env.session, provider_id="LOCAL", display_name="Local B")
    drive = make_account(
        env.session,
        provider_id="GOOGLE_DRIVE",
        display_name="Drive C",
        credentials_ref="ref-c",
    )

    response = env.client.get(BASE_URL, params={"provider_id": "GOOGLE_DRIVE"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [drive.id]
    assert body["items"][0]["provider_id"] == "GOOGLE_DRIVE"


@pytest.mark.parametrize(
    "provider_id",
    ["local", "Local", "1LOCAL", "LOCAL ", " LOCAL", "LOC-AL", ""],
)
def test_list_rejects_invalid_provider_filter(env, provider_id):
    make_account(env.session, provider_id="LOCAL", display_name="Local A")

    response = env.client.get(BASE_URL, params={"provider_id": provider_id})

    assert response.status_code == 400
    assert "provider" in response.json()["detail"].lower()


def test_list_returns_empty_collection(env):
    response = env.client.get(BASE_URL)

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


# ---------------------------------------------------------------------------
# Detail endpoint
# ---------------------------------------------------------------------------


def test_get_existing_account(env):
    account = make_account(
        env.session,
        provider_id="LOCAL",
        display_name="Local A",
        configuration={"storage_root": "/data"},
    )

    response = env.client.get(f"{BASE_URL}/{account.id}")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == RESPONSE_KEYS
    assert body["id"] == account.id
    assert body["provider_id"] == "LOCAL"
    assert body["display_name"] == "Local A"
    assert body["status"] == "pending"
    assert body["configuration"] == {"storage_root": "/data"}


def test_get_unknown_account_returns_404(env):
    response = env.client.get(f"{BASE_URL}/does-not-exist")

    assert response.status_code == 404
    assert "was not found" in response.json()["detail"]


# ---------------------------------------------------------------------------
# No secrets, no shared mutable state, no cache
# ---------------------------------------------------------------------------


def test_responses_never_expose_credentials_ref_or_secrets(env):
    secret_ref = "storage-account:super-secret-ref-123"
    account = make_account(
        env.session,
        provider_id="GOOGLE_DRIVE",
        display_name="Drive A",
        credentials_ref=secret_ref,
    )

    listing = env.client.get(BASE_URL)
    detail = env.client.get(f"{BASE_URL}/{account.id}")

    assert listing.status_code == 200
    assert detail.status_code == 200
    for response in (listing, detail):
        assert secret_ref not in response.text
        assert "credentials_ref" not in response.text
        assert "refresh_token" not in response.text
        assert "access_token" not in response.text
        assert "client_secret" not in response.text

    assert "credentials_ref" not in listing.json()["items"][0]
    assert "credentials_ref" not in detail.json()


def test_response_configuration_is_detached_from_orm_state(env):
    account = make_account(
        env.session,
        provider_id="LOCAL",
        display_name="Local A",
        configuration={"nested": {"a": 1}},
    )

    # Call the handler directly so the returned model (not just its JSON form)
    # can be mutated; a shared dict would corrupt the ORM instance.
    result = asyncio.run(get_storage_account(account.id, db=env.session))
    result.configuration["nested"]["a"] = 999

    env.session.expire_all()
    reloaded = env.session.get(StorageAccount, account.id)
    assert reloaded.configuration == {"nested": {"a": 1}}


def test_get_reads_current_state_on_every_call(env):
    account = make_account(
        env.session, provider_id="LOCAL", display_name="Original Name"
    )

    first = env.client.get(f"{BASE_URL}/{account.id}")
    AccountManager(env.session).update_account(
        account.id, display_name="Renamed Account"
    )
    second = env.client.get(f"{BASE_URL}/{account.id}")

    assert first.json()["display_name"] == "Original Name"
    assert second.json()["display_name"] == "Renamed Account"
