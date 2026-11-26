"""Focused tests for the HVSA H1.5.3 storage account create endpoint.

POST /api/v1/storage-accounts

Each test uses an isolated in-memory SQLite database wired into the app through
``dependency_overrides`` — the real application database is never touched.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.v1.endpoints import storage_accounts as storage_accounts_endpoints
from backend.api.v1.endpoints.storage_accounts import create_storage_account
from backend.database import Base, get_db
from backend.main import app
from backend.models import StorageAccount
from backend.schemas import StorageAccountCreate
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
            session_factory=TestSession,
            engine=engine,
        )
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


def payload(**overrides) -> dict:
    body = {
        "provider_id": "GOOGLE_DRIVE",
        "display_name": "Drive Account",
        "configuration": {},
    }
    body.update(overrides)
    return body


def fetch_accounts(env) -> list[dict]:
    """Read the persisted rows through a fresh session (server-side truth)."""
    session = env.session_factory()
    try:
        return [
            {
                "id": row.id,
                "provider_id": row.provider_id,
                "display_name": row.display_name,
                "credentials_ref": row.credentials_ref,
                "status": row.status,
                "configuration": row.configuration,
            }
            for row in session.query(StorageAccount).all()
        ]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Successful creation
# ---------------------------------------------------------------------------


def test_create_storage_account(env):
    response = env.client.post(
        BASE_URL,
        json=payload(
            display_name="  Drive Account  ",
            configuration={"folder_id": "abc123"},
        ),
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == RESPONSE_KEYS
    uuid.UUID(body["id"])
    assert body["provider_id"] == "GOOGLE_DRIVE"
    assert body["display_name"] == "Drive Account"
    assert body["configuration"] == {"folder_id": "abc123"}

    rows = fetch_accounts(env)
    assert len(rows) == 1
    assert rows[0]["id"] == body["id"]
    assert rows[0]["display_name"] == "Drive Account"
    assert rows[0]["configuration"] == {"folder_id": "abc123"}


def test_create_generates_credentials_ref_on_the_server(env):
    response = env.client.post(BASE_URL, json=payload())

    assert response.status_code == 201
    rows = fetch_accounts(env)
    assert len(rows) == 1
    ref = rows[0]["credentials_ref"]
    assert ref is not None
    assert ref.startswith("storage-account:")
    # The suffix is a server-generated UUID, not the client's account id.
    uuid.UUID(ref.split(":", 1)[1])
    assert ref not in response.text


def test_create_starts_in_pending_status(env):
    response = env.client.post(BASE_URL, json=payload())

    assert response.status_code == 201
    assert response.json()["status"] == "pending"
    assert fetch_accounts(env)[0]["status"] == "pending"


def test_create_defaults_to_empty_configuration(env):
    body = payload()
    del body["configuration"]

    response = env.client.post(BASE_URL, json=body)

    assert response.status_code == 201
    assert response.json()["configuration"] == {}
    assert fetch_accounts(env)[0]["configuration"] == {}


def test_create_supports_local_accounts(env):
    response = env.client.post(
        BASE_URL,
        json=payload(provider_id="LOCAL", display_name="Local A"),
    )

    assert response.status_code == 201
    assert response.json()["provider_id"] == "LOCAL"


def test_create_response_never_exposes_credentials_or_secrets(env):
    response = env.client.post(BASE_URL, json=payload())

    assert response.status_code == 201
    body = response.json()
    assert "credentials_ref" not in body
    for marker in (
        "credentials_ref",
        "refresh_token",
        "access_token",
        "client_secret",
        "storage-account:",
    ):
        assert marker not in response.text


# ---------------------------------------------------------------------------
# Rejected input
# ---------------------------------------------------------------------------


def test_create_rejects_client_supplied_credentials_ref(env):
    response = env.client.post(
        BASE_URL,
        json=payload(credentials_ref="storage-account:attacker-chosen"),
    )

    assert response.status_code == 422
    assert fetch_accounts(env) == []


@pytest.mark.parametrize(
    "provider_id",
    ["local", "Local", "1LOCAL", "LOCAL ", " LOCAL", "LOC-AL", ""],
)
def test_create_rejects_invalid_provider(env, provider_id):
    response = env.client.post(BASE_URL, json=payload(provider_id=provider_id))

    assert response.status_code == 422
    assert fetch_accounts(env) == []


@pytest.mark.parametrize("display_name", ["", "   ", "x" * 256, None])
def test_create_rejects_invalid_display_name(env, display_name):
    response = env.client.post(BASE_URL, json=payload(display_name=display_name))

    assert response.status_code == 422
    assert fetch_accounts(env) == []


def test_create_validates_provider_through_account_manager(env):
    # Bypass schema validation to prove the endpoint re-validates the provider
    # through AccountManager rather than trusting the payload.
    invalid = StorageAccountCreate.model_construct(
        provider_id="lowercase",
        display_name="Local A",
        configuration={},
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(create_storage_account(invalid, db=env.session))

    assert excinfo.value.status_code == 422
    assert fetch_accounts(env) == []


def test_create_rejects_non_json_serializable_configuration(env):
    response = env.client.post(
        BASE_URL,
        content=(
            '{"provider_id": "LOCAL", "display_name": "Local A", '
            '"configuration": {"ratio": NaN}}'
        ),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert "JSON-serializable" in response.json()["detail"]
    assert fetch_accounts(env) == []


def test_create_rejects_non_json_python_values(env):
    # Direct handler call: an HTTP client cannot send a set, but the guard
    # must still reject it before SQLAlchemy would raise a 500.
    invalid = StorageAccountCreate.model_construct(
        provider_id="LOCAL",
        display_name="Local A",
        configuration={"bad": {"not-json"}},
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(create_storage_account(invalid, db=env.session))

    assert excinfo.value.status_code == 422
    assert fetch_accounts(env) == []


# ---------------------------------------------------------------------------
# Conflict handling and rollback
# ---------------------------------------------------------------------------


def test_create_rejects_duplicate_credentials_ref(env, monkeypatch):
    fixed_ref = "storage-account:11111111-1111-1111-1111-111111111111"
    AccountManager(env.session).create_account(
        provider_id="LOCAL",
        display_name="Existing Account",
        credentials_ref=fixed_ref,
        configuration={},
    )
    monkeypatch.setattr(
        storage_accounts_endpoints, "_new_credentials_ref", lambda: fixed_ref
    )

    response = env.client.post(BASE_URL, json=payload())

    assert response.status_code == 409
    assert "credential reference" in response.json()["detail"].lower()
    assert fixed_ref not in response.text
    rows = fetch_accounts(env)
    assert len(rows) == 1
    assert rows[0]["display_name"] == "Existing Account"


def test_create_failure_rolls_back_and_leaves_no_partial_account(env, monkeypatch):
    original_flush = Session.flush

    def failing_commit(self):
        # Flush the INSERT, then fail: the row reaches the database inside the
        # transaction but must be discarded by the endpoint's rollback.
        original_flush(self)
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(Session, "commit", failing_commit)

    response = env.client.post(BASE_URL, json=payload())

    assert response.status_code == 500
    assert fetch_accounts(env) == []

    monkeypatch.undo()
    retry = env.client.post(BASE_URL, json=payload())
    assert retry.status_code == 201
    assert len(fetch_accounts(env)) == 1
