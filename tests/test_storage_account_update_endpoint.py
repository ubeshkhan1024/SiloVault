"""Focused tests for the HVSA H1.5.3 storage account update endpoint.

PATCH /api/v1/storage-accounts/{account_id}

Each test uses an isolated in-memory SQLite database wired into the app through
``dependency_overrides`` — the real application database is never touched.
Cache invalidation is observed by wrapping the central
``AccountAdapterManager.invalidate_account`` so the assertions are about the
real callback the endpoint passes, not a stub.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.v1.endpoints.storage_accounts import update_storage_account
from backend.database import Base, get_db
from backend.main import app
from backend.models import StorageAccount
from backend.schemas import StorageAccountUpdate
from backend.storage.account_adapter_manager import adapter_manager
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


@pytest.fixture
def invalidations(monkeypatch):
    """Record every account id whose cached adapter is invalidated."""
    calls: list[str] = []
    original = adapter_manager.invalidate_account

    def record(account_id):
        calls.append(account_id)
        original(account_id)

    monkeypatch.setattr(adapter_manager, "invalidate_account", record)
    return calls


def make_account(
    session,
    *,
    provider_id="LOCAL",
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


# ---------------------------------------------------------------------------
# Successful updates
# ---------------------------------------------------------------------------


def test_update_display_name(env, invalidations):
    account = make_account(
        env.session,
        provider_id="GOOGLE_DRIVE",
        display_name="Old Name",
        credentials_ref="ref-a",
        configuration={"folder_id": "abc"},
    )

    response = env.client.patch(
        f"{BASE_URL}/{account.id}", json={"display_name": "  New Name  "}
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == RESPONSE_KEYS
    assert body["id"] == account.id
    assert body["display_name"] == "New Name"
    # Untouched fields are unchanged, including the provider and the config.
    assert body["provider_id"] == "GOOGLE_DRIVE"
    assert body["configuration"] == {"folder_id": "abc"}
    assert "credentials_ref" not in response.text

    row = fetch_row(env, account.id)
    assert row["display_name"] == "New Name"
    assert row["provider_id"] == "GOOGLE_DRIVE"
    assert row["credentials_ref"] == "ref-a"
    assert row["configuration"] == {"folder_id": "abc"}
    # A display-name-only change never touches the account's adapter.
    assert invalidations == []


def test_update_local_storage_root_invalidates(env, invalidations):
    account = make_account(
        env.session,
        provider_id="LOCAL",
        display_name="Local A",
        configuration={"storage_root": "/data/old", "label": "keep"},
    )

    response = env.client.patch(
        f"{BASE_URL}/{account.id}",
        json={"configuration": {"storage_root": "/data/new"}},
    )

    assert response.status_code == 200
    assert response.json()["configuration"] == {"storage_root": "/data/new"}
    assert fetch_row(env, account.id)["configuration"] == {
        "storage_root": "/data/new"
    }
    assert invalidations == [account.id]


def test_update_unrelated_configuration_does_not_invalidate(env, invalidations):
    account = make_account(
        env.session,
        provider_id="LOCAL",
        display_name="Local A",
        configuration={"storage_root": "/data", "note": "old"},
    )

    response = env.client.patch(
        f"{BASE_URL}/{account.id}",
        json={"configuration": {"storage_root": "/data", "note": "new"}},
    )

    assert response.status_code == 200
    assert response.json()["configuration"] == {
        "storage_root": "/data",
        "note": "new",
    }
    assert invalidations == []


def test_update_equivalent_storage_root_does_not_invalidate(env, invalidations):
    account = make_account(
        env.session,
        provider_id="LOCAL",
        display_name="Local A",
        configuration={"storage_root": "/data/storage"},
    )

    # Same path, different string spelling: Path comparison in AccountManager
    # treats this as unchanged, so the cached adapter must survive.
    response = env.client.patch(
        f"{BASE_URL}/{account.id}",
        json={"configuration": {"storage_root": "/data/storage/"}},
    )

    assert response.status_code == 200
    assert response.json()["configuration"] == {"storage_root": "/data/storage/"}
    assert invalidations == []


def test_update_non_local_configuration_does_not_invalidate(env, invalidations):
    account = make_account(
        env.session,
        provider_id="GOOGLE_DRIVE",
        display_name="Drive A",
        credentials_ref="ref-a",
        configuration={"storage_root": "/data/old"},
    )

    response = env.client.patch(
        f"{BASE_URL}/{account.id}",
        json={"configuration": {"storage_root": "/data/new"}},
    )

    assert response.status_code == 200
    assert response.json()["configuration"] == {"storage_root": "/data/new"}
    assert invalidations == []


# ---------------------------------------------------------------------------
# No-op updates
# ---------------------------------------------------------------------------


def test_empty_patch_is_a_noop(env, invalidations):
    account = make_account(
        env.session,
        provider_id="LOCAL",
        display_name="Local A",
        configuration={"storage_root": "/data"},
    )
    before = fetch_row(env, account.id)

    response = env.client.patch(f"{BASE_URL}/{account.id}", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "Local A"
    assert body["configuration"] == {"storage_root": "/data"}
    # No UPDATE was issued, so not even the timestamp moved.
    assert fetch_row(env, account.id) == before
    assert invalidations == []


def test_patch_without_a_body_is_a_noop(env, invalidations):
    account = make_account(env.session, display_name="Local A")
    before = fetch_row(env, account.id)

    response = env.client.patch(f"{BASE_URL}/{account.id}")

    assert response.status_code == 200
    assert response.json()["display_name"] == "Local A"
    assert fetch_row(env, account.id) == before
    assert invalidations == []


def test_explicit_null_keeps_existing_values(env, invalidations):
    account = make_account(
        env.session,
        provider_id="LOCAL",
        display_name="Keep Me",
        configuration={"storage_root": "/data"},
    )

    response = env.client.patch(
        f"{BASE_URL}/{account.id}",
        json={"display_name": None, "configuration": None},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "Keep Me"
    assert body["configuration"] == {"storage_root": "/data"}
    row = fetch_row(env, account.id)
    assert row["display_name"] == "Keep Me"
    assert row["configuration"] == {"storage_root": "/data"}
    assert invalidations == []


def test_explicit_null_configuration_with_name_change(env, invalidations):
    account = make_account(
        env.session,
        provider_id="LOCAL",
        display_name="Old",
        configuration={"storage_root": "/data"},
    )

    response = env.client.patch(
        f"{BASE_URL}/{account.id}",
        json={"display_name": "New", "configuration": None},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "New"
    assert body["configuration"] == {"storage_root": "/data"}
    assert invalidations == []


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_update_unknown_account_returns_404(env, invalidations):
    response = env.client.patch(
        f"{BASE_URL}/does-not-exist", json={"display_name": "Nope"}
    )

    assert response.status_code == 404
    assert "was not found" in response.json()["detail"]
    assert invalidations == []


@pytest.mark.parametrize(
    "extra_field",
    [
        {"provider_id": "GOOGLE_DRIVE"},
        {"provider_id": "LOCAL"},
        {"credentials_ref": "storage-account:attacker-chosen"},
        {"credentials_ref": None},
    ],
)
def test_update_rejects_identity_and_credential_fields(env, invalidations, extra_field):
    account = make_account(
        env.session,
        provider_id="LOCAL",
        display_name="Local A",
        configuration={"storage_root": "/data"},
    )
    before = fetch_row(env, account.id)

    response = env.client.patch(
        f"{BASE_URL}/{account.id}",
        json={"display_name": "Changed", **extra_field},
    )

    assert response.status_code == 422
    assert fetch_row(env, account.id) == before
    assert invalidations == []


@pytest.mark.parametrize("display_name", ["", "   ", "x" * 256])
def test_update_rejects_invalid_display_name(env, invalidations, display_name):
    account = make_account(env.session, display_name="Local A")
    before = fetch_row(env, account.id)

    response = env.client.patch(
        f"{BASE_URL}/{account.id}", json={"display_name": display_name}
    )

    assert response.status_code == 422
    assert fetch_row(env, account.id) == before
    assert invalidations == []


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_update_rejects_non_finite_configuration(env, invalidations, literal):
    account = make_account(
        env.session,
        provider_id="LOCAL",
        display_name="Local A",
        configuration={"storage_root": "/data"},
    )
    before = fetch_row(env, account.id)

    response = env.client.patch(
        f"{BASE_URL}/{account.id}",
        content=f'{{"configuration": {{"ratio": {literal}}}}}',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert "JSON-serializable" in response.json()["detail"]
    assert fetch_row(env, account.id) == before
    assert invalidations == []


def test_update_rejects_non_json_python_values(env, invalidations):
    # Direct handler call: an HTTP client cannot send a set, but the guard must
    # still reject it before SQLAlchemy would raise a 500.
    account = make_account(
        env.session,
        provider_id="LOCAL",
        display_name="Local A",
        configuration={"storage_root": "/data"},
    )
    invalid = StorageAccountUpdate.model_construct(
        configuration={"bad": {"not-json"}}
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            update_storage_account(account.id, payload=invalid, db=env.session)
        )

    assert excinfo.value.status_code == 422
    assert fetch_row(env, account.id)["configuration"] == {"storage_root": "/data"}
    assert invalidations == []


# ---------------------------------------------------------------------------
# Commit failure and cache isolation
# ---------------------------------------------------------------------------


def test_commit_failure_rolls_back_and_does_not_invalidate(
    env, invalidations, monkeypatch
):
    account = make_account(
        env.session,
        provider_id="LOCAL",
        display_name="Original",
        configuration={"storage_root": "/data/old"},
    )
    original_flush = Session.flush
    original_commit = Session.commit

    def failing_commit(self):
        # Flush the UPDATE, then fail: the change reaches the transaction but
        # must be discarded by the endpoint's rollback.
        original_flush(self)
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(Session, "commit", failing_commit)

    response = env.client.patch(
        f"{BASE_URL}/{account.id}",
        json={"configuration": {"storage_root": "/data/new"}},
    )

    assert response.status_code == 500
    assert invalidations == []
    row = fetch_row(env, account.id)
    assert row["display_name"] == "Original"
    assert row["configuration"] == {"storage_root": "/data/old"}

    monkeypatch.setattr(Session, "commit", original_commit)
    retry = env.client.patch(
        f"{BASE_URL}/{account.id}", json={"display_name": "Recovered"}
    )
    assert retry.status_code == 200
    assert fetch_row(env, account.id)["display_name"] == "Recovered"


def test_update_invalidates_only_the_updated_account(env, invalidations):
    first = make_account(
        env.session,
        provider_id="LOCAL",
        display_name="First",
        configuration={"storage_root": "/data/one"},
    )
    second = make_account(
        env.session,
        provider_id="LOCAL",
        display_name="Second",
        configuration={"storage_root": "/data/two"},
    )
    first_adapter = object()
    second_adapter = object()
    adapter_manager.install(first.id, first_adapter)
    adapter_manager.install(second.id, second_adapter)

    response = env.client.patch(
        f"{BASE_URL}/{first.id}",
        json={"configuration": {"storage_root": "/data/moved"}},
    )

    assert response.status_code == 200
    assert invalidations == [first.id]
    assert adapter_manager.cache.get(first.id) is None
    assert adapter_manager.cache.get(second.id) is second_adapter
