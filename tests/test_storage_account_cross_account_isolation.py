"""Cross-account isolation tests for the HVSA H1.5.3 storage-account API.

Covers the HTTP boundary of ``/api/v1/storage-accounts``:

* two accounts of the same provider are independent records,
* server-generated IDs and credential references are distinct and never
  returned,
* reads and writes for one account never change another account,
* creating an account never evicts another account's cached adapter,
* CRUD works regardless of the account's lifecycle status,
* unknown accounts return 404 and empty provider filters return an empty list.

Every test uses an isolated in-memory SQLite database wired into the app
through ``dependency_overrides``; the real application database, provider
services, OAuth, and the OS keyring are never touched.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base, get_db
from backend.main import app
from backend.models import StorageAccount
from backend.storage import registry as registry_mod
from backend.storage.account_adapter_manager import adapter_manager
from backend.storage.account_manager import AccountManager
from backend.storage.errors import StorageUnavailableError

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

SECRET_MARKERS = (
    "credentials_ref",
    "refresh_token",
    "access_token",
    "client_secret",
    "storage-account:",
)


@pytest.fixture
def env():
    """TestClient wired to an isolated in-memory DB with a clean adapter cache."""
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
    adapter_manager.invalidate_all()

    try:
        yield SimpleNamespace(
            client=TestClient(app, raise_server_exceptions=False),
            session=session,
            session_factory=TestSession,
            engine=engine,
        )
    finally:
        app.dependency_overrides.clear()
        session.close()
        adapter_manager.invalidate_all()
        engine.dispose()


def payload(**overrides) -> dict:
    body = {
        "provider_id": "GOOGLE_DRIVE",
        "display_name": "Drive Account",
        "configuration": {},
    }
    body.update(overrides)
    return body


def create(env, **overrides) -> dict:
    response = env.client.post(BASE_URL, json=payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


def fetch_row(env, account_id: str) -> dict:
    """Read one persisted row through a fresh session (server-side truth)."""
    session = env.session_factory()
    try:
        row = session.get(StorageAccount, account_id)
        assert row is not None
        return {
            "id": row.id,
            "provider_id": row.provider_id,
            "display_name": row.display_name,
            "credentials_ref": row.credentials_ref,
            "status": row.status,
            "configuration": row.configuration,
            "updated_at": row.updated_at,
        }
    finally:
        session.close()


def set_status(env, account_id: str, status: str) -> None:
    session = env.session_factory()
    try:
        row = session.get(StorageAccount, account_id)
        assert row is not None
        row.status = status
        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Independent creation of same-provider accounts
# ---------------------------------------------------------------------------


def test_two_accounts_of_the_same_provider_are_created_independently(env):
    first = create(env, display_name="Drive A", configuration={"folder_id": "a"})
    second = create(env, display_name="Drive B", configuration={"folder_id": "b"})

    assert first["provider_id"] == second["provider_id"] == "GOOGLE_DRIVE"
    assert first["id"] != second["id"]
    uuid.UUID(first["id"])
    uuid.UUID(second["id"])

    rows = {row["id"]: row for row in map(lambda aid: fetch_row(env, aid), [first["id"], second["id"]])}
    assert set(rows) == {first["id"], second["id"]}
    assert rows[first["id"]]["display_name"] == "Drive A"
    assert rows[second["id"]]["display_name"] == "Drive B"
    assert rows[first["id"]]["configuration"] == {"folder_id": "a"}
    assert rows[second["id"]]["configuration"] == {"folder_id": "b"}


def test_each_account_gets_a_distinct_server_generated_reference(env):
    first = create(env, display_name="Drive A")
    second = create(env, display_name="Drive B")

    ref_a = fetch_row(env, first["id"])["credentials_ref"]
    ref_b = fetch_row(env, second["id"])["credentials_ref"]

    assert ref_a is not None and ref_b is not None
    assert ref_a.startswith("storage-account:")
    assert ref_b.startswith("storage-account:")
    uuid.UUID(ref_a.split(":", 1)[1])
    uuid.UUID(ref_b.split(":", 1)[1])
    assert ref_a != ref_b
    # The suffix is a fresh server UUID, not the account's own id.
    assert ref_a != f"storage-account:{first['id']}"
    assert ref_b != f"storage-account:{second['id']}"


def test_credential_references_never_appear_in_api_responses(env):
    first = create(env, display_name="Drive A")
    second = create(env, display_name="Drive B")
    ref_a = fetch_row(env, first["id"])["credentials_ref"]
    ref_b = fetch_row(env, second["id"])["credentials_ref"]

    responses = [
        env.client.get(BASE_URL),
        env.client.get(f"{BASE_URL}/{first['id']}"),
        env.client.get(f"{BASE_URL}/{second['id']}"),
        env.client.patch(f"{BASE_URL}/{first['id']}", json={"display_name": "Renamed"}),
    ]

    for response in responses:
        assert response.status_code == 200
        assert ref_a not in response.text
        assert ref_b not in response.text
        for marker in SECRET_MARKERS:
            assert marker not in response.text


# ---------------------------------------------------------------------------
# Reads stay scoped to the addressed account
# ---------------------------------------------------------------------------


def test_detail_returns_only_the_addressed_accounts_information(env):
    first = create(env, display_name="Drive A", configuration={"folder_id": "a"})
    second = create(env, display_name="Drive B", configuration={"folder_id": "b"})

    body_a = env.client.get(f"{BASE_URL}/{first['id']}").json()
    body_b = env.client.get(f"{BASE_URL}/{second['id']}").json()

    assert set(body_a) == RESPONSE_KEYS
    assert set(body_b) == RESPONSE_KEYS
    assert body_a["id"] == first["id"]
    assert body_a["display_name"] == "Drive A"
    assert body_a["configuration"] == {"folder_id": "a"}
    assert body_b["id"] == second["id"]
    assert body_b["display_name"] == "Drive B"
    assert body_b["configuration"] == {"folder_id": "b"}
    assert body_a["display_name"] != body_b["display_name"]


def test_list_never_exposes_credentials_tokens_or_secrets(env):
    create(env, display_name="Drive A")
    create(env, display_name="Drive B", provider_id="LOCAL", configuration={"storage_root": "/data"})

    response = env.client.get(BASE_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    for item in body["items"]:
        assert set(item) == RESPONSE_KEYS
    for marker in SECRET_MARKERS:
        assert marker not in response.text


# ---------------------------------------------------------------------------
# Writes stay scoped to the addressed account
# ---------------------------------------------------------------------------


def test_patch_of_a_leaves_bs_row_configuration_and_timestamp_unchanged(env):
    first = create(env, display_name="Drive A", configuration={"folder_id": "a"})
    second = create(env, display_name="Drive B", configuration={"folder_id": "b"})
    before_b = fetch_row(env, second["id"])

    response = env.client.patch(
        f"{BASE_URL}/{first['id']}",
        json={"display_name": "Drive A Renamed", "configuration": {"folder_id": "a2"}},
    )

    assert response.status_code == 200
    assert response.json()["display_name"] == "Drive A Renamed"
    assert fetch_row(env, first["id"])["display_name"] == "Drive A Renamed"
    # Every persisted field of B, including updated_at, is untouched.
    assert fetch_row(env, second["id"]) == before_b


def test_updating_one_local_account_preserves_the_others_storage_root(env):
    first = create(env, provider_id="LOCAL", display_name="Local A", configuration={"storage_root": "/data/a"})
    second = create(env, provider_id="LOCAL", display_name="Local B", configuration={"storage_root": "/data/b"})

    response = env.client.patch(
        f"{BASE_URL}/{first['id']}",
        json={"configuration": {"storage_root": "/data/a-moved"}},
    )

    assert response.status_code == 200
    assert response.json()["configuration"] == {"storage_root": "/data/a-moved"}
    assert fetch_row(env, first["id"])["configuration"] == {"storage_root": "/data/a-moved"}
    assert fetch_row(env, second["id"])["configuration"] == {"storage_root": "/data/b"}


def test_creating_an_account_does_not_invalidate_another_cached_adapter(env):
    first = create(env, display_name="Drive A")
    cached_a = object()
    adapter_manager.install(first["id"], cached_a)

    create(env, display_name="Drive B")

    assert adapter_manager.cache.get(first["id"]) is cached_a
    assert len(adapter_manager.cache) == 1


# ---------------------------------------------------------------------------
# Status-independent CRUD
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "connected", "disconnected", "error"])
def test_crud_works_for_every_lifecycle_status(env, status):
    created = create(env, display_name="Drive A", configuration={"folder_id": "a"})
    set_status(env, created["id"], status)

    detail = env.client.get(f"{BASE_URL}/{created['id']}")
    listing = env.client.get(BASE_URL, params={"provider_id": "GOOGLE_DRIVE"})
    updated = env.client.patch(
        f"{BASE_URL}/{created['id']}", json={"display_name": "Drive A Renamed"}
    )

    assert detail.status_code == 200
    assert detail.json()["status"] == status
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()["items"]] == [created["id"]]
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "Drive A Renamed"
    # A display-name change is status-independent and preserves the status.
    assert updated.json()["status"] == status
    assert fetch_row(env, created["id"])["status"] == status


# ---------------------------------------------------------------------------
# Not-found and filter boundaries
# ---------------------------------------------------------------------------


def test_unknown_account_returns_404_on_detail_and_update(env):
    detail = env.client.get(f"{BASE_URL}/does-not-exist")
    update = env.client.patch(
        f"{BASE_URL}/does-not-exist", json={"display_name": "Nope"}
    )

    assert detail.status_code == 404
    assert update.status_code == 404
    assert "was not found" in detail.json()["detail"]
    assert "was not found" in update.json()["detail"]


def test_valid_provider_filter_with_no_matches_returns_empty_list(env):
    create(env, provider_id="LOCAL", display_name="Local A", configuration={"storage_root": "/data"})

    response = env.client.get(BASE_URL, params={"provider_id": "GOOGLE_DRIVE"})

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


# ---------------------------------------------------------------------------
# Business endpoints stay provider-scoped
# ---------------------------------------------------------------------------


def test_managed_accounts_never_enter_the_provider_registry(env):
    """Business file endpoints dispatch through ``lookup(record.provider)``.

    Account-scoped routing is not implemented (and is deliberately out of
    scope for H1.5.3), so installing a managed adapter must never make the
    provider registry return it and must never let a file request silently
    select a managed account. This asserts the boundary that keeps that true.
    """
    account = create(env, display_name="Drive A")
    managed_adapter = object()
    adapter_manager.install(account["id"], managed_adapter)

    assert adapter_manager.cache.get(account["id"]) is managed_adapter
    registered = registry_mod._adapter_map.get("GOOGLE_DRIVE")
    assert registered is not managed_adapter
    if registered is None:
        with pytest.raises(StorageUnavailableError):
            registry_mod.lookup("GOOGLE_DRIVE")
