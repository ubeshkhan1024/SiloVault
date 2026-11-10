"""Tests for the files endpoints.

Covers the real database-backed ``GET /api/v1/files`` listing endpoint and
``GET /api/v1/files/{file_id}`` lookup endpoint using isolated in-memory
SQLite databases (via a ``get_db`` dependency override), so the development
database is never touched.
"""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base, get_db
from backend.main import app
from backend.models.unified_file import UnifiedFileRecord


@pytest.fixture
def files_client():
    """Yields a TestClient wired to an isolated in-memory SQLite database."""
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
    try:
        yield TestClient(app), engine
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _seed_file(
    session,
    provider="LOCAL",
    provider_account_id=None,
    provider_file_id=None,
    name="doc.pdf",
    category=None,
    sync_state="PENDING",
    created_at=None,
    **kwargs,
):
    """Inserts a UnifiedFileRecord with sensible defaults.

    Callers may override any field via ``**kwargs``; ``size_bytes`` defaults
    to ``1024`` unless a caller supplies its own value.

    The returned record is refreshed against the database before being handed
    back, ensuring its primary key is populated and the object remains usable
    after the parent session is closed.
    """
    size_bytes = kwargs.pop("size_bytes", 1024)
    record = UnifiedFileRecord(
        provider=provider,
        provider_account_id=provider_account_id,
        provider_file_id=provider_file_id or f"{provider.lower()}_{name}",
        name=name,
        logical_path=f"/{name}",
        mime_type="application/pdf",
        size_bytes=size_bytes,
        category=category,
        sync_state=sync_state,
        created_at=created_at or datetime.now(timezone.utc),
        **kwargs,
    )
    session.add(record)
    session.flush()  # ensure id is generated before commit/refresh
    session.commit()
    session.refresh(record)  # bind the instance to this session for safe post-close access
    return record


def test_list_files_empty_database_returns_empty_list(files_client):
    """An empty database must return an empty items list with total=0."""
    client, engine = files_client
    response = client.get("/api/v1/files")

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"] == []
    assert payload["total"] == 0
    assert payload["limit"] == 50
    assert payload["offset"] == 0


def test_list_files_returns_inserted_records(files_client):
    """Inserted records must appear in the response with full metadata."""
    client, engine = files_client
    session = sessionmaker(bind=engine)()
    try:
        _seed_file(
            session,
            provider="GOOGLE_DRIVE",
            provider_account_id="acc_1",
            name="report.docx",
            category="Documents",
            sync_state="COMPLETED",
            tags=["work", "2026"],
            sha256="ab" * 32,
        )
    finally:
        session.close()

    response = client.get("/api/v1/files")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert len(payload["items"]) == 1

    item = payload["items"][0]
    assert item["id"].startswith("file_")
    assert item["provider"] == "GOOGLE_DRIVE"
    assert item["provider_account_id"] == "acc_1"
    assert item["name"] == "report.docx"
    assert item["logical_path"] == "/report.docx"
    assert item["mime_type"] == "application/pdf"
    assert item["size_bytes"] == 1024
    assert item["sha256"] == "ab" * 32
    assert item["category"] == "Documents"
    assert item["tags"] == ["work", "2026"]
    assert item["sync_state"] == "COMPLETED"
    assert item["created_at"].endswith("Z")
    assert "updated_at" in item


def test_list_files_pagination_works(files_client):
    """Pagination via limit and offset must slice results correctly."""
    client, engine = files_client
    session = sessionmaker(bind=engine)()
    try:
        for i in range(5):
            _seed_file(session, name=f"file_{i}.txt")
    finally:
        session.close()

    # First page
    response = client.get("/api/v1/files", params={"limit": 2, "offset": 0})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 5
    assert len(payload["items"]) == 2
    assert payload["limit"] == 2
    assert payload["offset"] == 0

    # Second page
    response = client.get("/api/v1/files", params={"limit": 2, "offset": 2})
    payload = response.json()
    assert len(payload["items"]) == 2
    assert payload["offset"] == 2

    # Last page (1 remaining)
    response = client.get("/api/v1/files", params={"limit": 2, "offset": 4})
    payload = response.json()
    assert len(payload["items"]) == 1
    assert payload["total"] == 5

    # No overlap between pages
    first_ids = {i["id"] for i in client.get("/api/v1/files", params={"limit": 2, "offset": 0}).json()["items"]}
    second_ids = {i["id"] for i in client.get("/api/v1/files", params={"limit": 2, "offset": 2}).json()["items"]}
    assert first_ids.isdisjoint(second_ids)


def test_list_files_provider_filter(files_client):
    """The provider filter must return only matching records."""
    client, engine = files_client
    session = sessionmaker(bind=engine)()
    try:
        _seed_file(session, provider="LOCAL", name="local.txt")
        _seed_file(session, provider="DROPBOX", provider_account_id="db_1", name="cloud.txt")
        _seed_file(session, provider="LOCAL", name="local2.txt")
    finally:
        session.close()

    response = client.get("/api/v1/files", params={"provider": "LOCAL"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert all(item["provider"] == "LOCAL" for item in payload["items"])

    response = client.get("/api/v1/files", params={"provider": "DROPBOX"})
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["provider"] == "DROPBOX"

    # Unknown provider returns empty
    response = client.get("/api/v1/files", params={"provider": "MEGA"})
    payload = response.json()
    assert payload["total"] == 0
    assert payload["items"] == []


def test_list_files_category_filter(files_client):
    """The category filter must return only matching records."""
    client, engine = files_client
    session = sessionmaker(bind=engine)()
    try:
        _seed_file(session, name="a.pdf", category="Financial/Receipts")
        _seed_file(session, name="b.pdf", category="Personal")
        _seed_file(session, name="c.pdf", category="Financial/Receipts")
        _seed_file(session, name="d.pdf", category=None)
    finally:
        session.close()

    response = client.get("/api/v1/files", params={"category": "Financial/Receipts"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert all(item["category"] == "Financial/Receipts" for item in payload["items"])


def test_list_files_sync_state_filter(files_client):
    """The sync_state filter must return only matching records."""
    client, engine = files_client
    session = sessionmaker(bind=engine)()
    try:
        _seed_file(session, name="a.txt", sync_state="PENDING")
        _seed_file(session, name="b.txt", sync_state="COMPLETED")
        _seed_file(session, name="c.txt", sync_state="PENDING")
    finally:
        session.close()

    response = client.get("/api/v1/files", params={"sync_state": "PENDING"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert all(item["sync_state"] == "PENDING" for item in payload["items"])

    response = client.get("/api/v1/files", params={"sync_state": "COMPLETED"})
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["name"] == "b.txt"


def test_list_files_total_reflects_filters(files_client):
    """The total count must reflect the applied filters, not the full table."""
    client, engine = files_client
    session = sessionmaker(bind=engine)()
    try:
        _seed_file(session, provider="LOCAL", name="a.txt", category="Work", sync_state="PENDING")
        _seed_file(session, provider="LOCAL", name="b.txt", category="Work", sync_state="PENDING")
        _seed_file(session, provider="LOCAL", name="c.txt", category="Work", sync_state="FAILED")
        _seed_file(session, provider="BOX", provider_account_id="box_1", name="d.txt", category="Work", sync_state="PENDING")
    finally:
        session.close()

    # No filters
    assert client.get("/api/v1/files").json()["total"] == 4
    # Single filter
    assert client.get("/api/v1/files", params={"provider": "LOCAL"}).json()["total"] == 3
    # Combined filters
    response = client.get(
        "/api/v1/files",
        params={"provider": "LOCAL", "category": "Work", "sync_state": "PENDING"},
    )
    assert response.json()["total"] == 2
    # Filter that matches nothing
    assert client.get("/api/v1/files", params={"sync_state": "DOWNLOADING"}).json()["total"] == 0


def test_list_files_invalid_pagination_returns_validation_error(files_client):
    """Invalid pagination values must produce 422 validation errors."""
    client, _ = files_client

    # limit below minimum
    response = client.get("/api/v1/files", params={"limit": 0})
    assert response.status_code == 422

    # limit above maximum
    response = client.get("/api/v1/files", params={"limit": 101})
    assert response.status_code == 422

    # negative offset
    response = client.get("/api/v1/files", params={"offset": -1})
    assert response.status_code == 422

    # non-integer limit
    response = client.get("/api/v1/files", params={"limit": "abc"})
    assert response.status_code == 422


def test_get_file_existing_id_returns_metadata(files_client):
    """An existing file ID must return the correct metadata."""
    client, engine = files_client
    session = sessionmaker(bind=engine)()
    try:
        record = _seed_file(
            session,
            provider="GOOGLE_DRIVE",
            provider_account_id="acc_99",
            name="invoice.pdf",
            category="Financial/Receipts",
            sync_state="COMPLETED",
            tags=["tax", "2026"],
            sha256="cd" * 32,
            size_bytes=9999,
        )
    finally:
        session.close()

    response = client.get(f"/api/v1/files/{record.id}")
    assert response.status_code == 200
    payload = response.json()

    assert payload["id"] == record.id
    assert payload["provider"] == "GOOGLE_DRIVE"
    assert payload["provider_account_id"] == "acc_99"
    assert payload["name"] == "invoice.pdf"
    assert payload["logical_path"] == "/invoice.pdf"
    assert payload["mime_type"] == "application/pdf"
    assert payload["size_bytes"] == 9999
    assert payload["sha256"] == "cd" * 32
    assert payload["category"] == "Financial/Receipts"
    assert payload["tags"] == ["tax", "2026"]
    assert payload["sync_state"] == "COMPLETED"
    assert payload["created_at"].endswith("Z")
    assert "updated_at" in payload


def test_get_file_nonexistent_id_returns_404(files_client):
    """A non-existent file ID must return HTTP 404 with a clear message."""
    client, _ = files_client
    response = client.get("/api/v1/files/file_does_not_exist_12345")

    assert response.status_code == 404
    payload = response.json()
    assert "detail" in payload
    assert "file_does_not_exist_12345" in payload["detail"]


def test_get_file_returns_tags_and_provider_metadata(files_client):
    """Returned metadata must include tags and provider metadata."""
    client, engine = files_client
    session = sessionmaker(bind=engine)()
    try:
        record = _seed_file(
            session,
            provider="DROPBOX",
            provider_account_id="db_acc",
            name="notes.txt",
            tags=["personal", "important", "review"],
            provider_metadata={
                "web_view_link": "https://dropbox.com/s/abc123",
                "revision": 7,
                "shared": True,
            },
        )
    finally:
        session.close()

    response = client.get(f"/api/v1/files/{record.id}")
    assert response.status_code == 200
    payload = response.json()

    assert payload["tags"] == ["personal", "important", "review"]
    assert payload["provider_metadata"] == {
        "web_view_link": "https://dropbox.com/s/abc123",
        "revision": 7,
        "shared": True,
    }


def test_list_endpoint_still_works_with_get_file(files_client):
    """The list endpoint must still function correctly alongside get_file."""
    client, engine = files_client
    session = sessionmaker(bind=engine)()
    try:
        record = _seed_file(session, name="test.txt", provider="LOCAL")
    finally:
        session.close()

    # List endpoint
    list_response = client.get("/api/v1/files")
    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["total"] == 1
    assert list_payload["items"][0]["id"] == record.id

    # Get by ID endpoint
    get_response = client.get(f"/api/v1/files/{record.id}")
    assert get_response.status_code == 200
    get_payload = get_response.json()
    assert get_payload["id"] == record.id
    assert get_payload["name"] == "test.txt"


def test_both_endpoints_work_with_empty_database(files_client):
    """Both endpoints must handle an empty database gracefully."""
    client, _ = files_client

    # List returns empty
    list_response = client.get("/api/v1/files")
    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["items"] == []
    assert list_payload["total"] == 0

    # Get returns 404
    get_response = client.get("/api/v1/files/file_nonexistent")
    assert get_response.status_code == 404
