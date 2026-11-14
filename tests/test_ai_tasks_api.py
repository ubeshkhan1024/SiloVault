"""Tests for the AI task API endpoints (M4.1 Stage 1B.1).

Covers POST/GET/DELETE on ``/api/v1/ai/tasks`` using an isolated
in-memory SQLite database (``StaticPool``) and the real FastAPI app with
``get_db`` overridden — the same fixture pattern as
``tests/test_file_rename_move.py``.

No worker, no inference, no Ollama, no model downloads: the tests only
exercise the API layer over the durable ``AiTask`` model. The development
database and real uploads directory are never touched.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import settings
from backend.database import Base, get_db
from backend.main import app
from backend.models.ai_task import AiTask
from backend.models.unified_file import UnifiedFileRecord
from backend.storage.capabilities import CAPABILITY_MAP
from backend.storage.local import LocalAdapter
from backend.storage.registry import register, remove


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ai_env(tmp_path, monkeypatch):
    """Yields an isolated test environment with a temp uploads dir and in-memory DB.

    Returns a dict with:
    - client: TestClient wired to the in-memory DB
    - engine: the SQLAlchemy engine
    - uploads_dir: Path to the temporary uploads directory
    """
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
    monkeypatch.setattr(settings, "uploads_dir", str(uploads_dir))

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

    # Register LOCAL adapter with the temp uploads directory (needed so the
    # app's storage registry is populated, matching the rename test fixture).
    adapter = LocalAdapter(str(uploads_dir))
    register("LOCAL", adapter, CAPABILITY_MAP["LOCAL"])

    try:
        yield {
            "client": TestClient(app),
            "engine": engine,
            "uploads_dir": uploads_dir,
        }
    finally:
        remove("LOCAL")
        app.dependency_overrides.clear()
        engine.dispose()


def _seed_file(engine, name: str = "notes.txt") -> UnifiedFileRecord:
    """Insert a LOCAL UnifiedFileRecord directly into the database."""
    session = sessionmaker(bind=engine)()
    try:
        record = UnifiedFileRecord(
            provider="LOCAL",
            provider_account_id=None,
            provider_file_id="seed_uuid_0001",
            name=name,
            logical_path=f"/{name}",
            mime_type="text/plain",
            size_bytes=11,
            sync_state="LOCAL",
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record
    finally:
        session.close()


def _seed_task(
    engine,
    file_id,
    status: str = "PENDING",
    task_type: str = "text_classification",
    **extra,
) -> AiTask:
    """Insert an AiTask row directly into the database (simulates states the
    API alone cannot reach, e.g. PROCESSING claimed by a future worker)."""
    session = sessionmaker(bind=engine)()
    try:
        task = AiTask(
            file_id=file_id,
            task_type=task_type,
            status=status,
            **extra,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        return task
    finally:
        session.close()


def _create_task(client, file_id: str, task_type: str = "text_classification") -> dict:
    """POST a new AI task and return the JSON payload."""
    response = client.post(
        "/api/v1/ai/tasks",
        json={"file_id": file_id, "task_type": task_type},
    )
    assert response.status_code == 201
    return response.json()


# ---------------------------------------------------------------------------
# POST /api/v1/ai/tasks
# ---------------------------------------------------------------------------


def test_create_task_success(ai_env):
    """Creating a task for an existing file returns 201 and a PENDING task."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_file(engine)

    body = _create_task(client, record.id)
    assert body["id"].startswith("task_")
    assert body["file_id"] == record.id
    assert body["task_type"] == "text_classification"
    assert body["status"] == "PENDING"
    assert body["retries"] == 0
    assert body["max_retries"] == 3
    assert body["proposal_json"] is None
    assert body["error_message"] is None
    assert body["started_at"] is None
    assert body["completed_at"] is None
    assert body["created_at"]


def test_create_task_missing_file_returns_404(ai_env):
    """Creating a task for a nonexistent file must return 404 and create nothing."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    response = client.post(
        "/api/v1/ai/tasks",
        json={"file_id": "file_does_not_exist_99999", "task_type": "text_classification"},
    )
    assert response.status_code == 404
    assert "detail" in response.json()

    # No task row was created.
    list_resp = client.get("/api/v1/ai/tasks")
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 0


def test_create_task_invalid_task_type_returns_422(ai_env):
    """An unknown task_type must be rejected with 422 and create nothing."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_file(engine)

    response = client.post(
        "/api/v1/ai/tasks",
        json={"file_id": record.id, "task_type": "image_generation"},
    )
    assert response.status_code == 422
    assert "detail" in response.json()

    list_resp = client.get("/api/v1/ai/tasks")
    assert list_resp.json()["total"] == 0


def test_create_task_missing_fields_returns_422(ai_env):
    """A request body missing required fields must be rejected with 422."""
    client = ai_env["client"]

    assert client.post("/api/v1/ai/tasks", json={}).status_code == 422
    assert (
        client.post("/api/v1/ai/tasks", json={"file_id": "file_x"}).status_code == 422
    )
    assert (
        client.post("/api/v1/ai/tasks", json={"task_type": "text_classification"}).status_code
        == 422
    )


def test_create_multiple_tasks_for_same_file_allowed(ai_env):
    """Multiple AI tasks may reference the same file (no unique constraint)."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_file(engine)

    first = _create_task(client, record.id)
    second = _create_task(client, record.id)

    assert first["id"] != second["id"]
    list_resp = client.get(f"/api/v1/ai/tasks?file_id={record.id}")
    assert list_resp.json()["total"] == 2


def test_create_task_does_not_touch_file_metadata(ai_env):
    """Task creation must not modify any original file metadata."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_file(engine)
    before = client.get(f"/api/v1/files/{record.id}").json()

    _create_task(client, record.id)

    after = client.get(f"/api/v1/files/{record.id}").json()
    assert before == after


def test_create_task_rejects_internal_fields_from_client(ai_env):
    """Clients cannot set internal claim/lease/status fields.

    Extra body fields are ignored by Pydantic; the stored task must use the
    server-controlled defaults (PENDING, no claim token, no worker).
    """
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_file(engine)

    response = client.post(
        "/api/v1/ai/tasks",
        json={
            "file_id": record.id,
            "task_type": "text_classification",
            "status": "SUCCEEDED",
            "claim_token": "forged_token",
            "worker_id": "forged_worker",
            "lease_expires_at": "2030-01-01T00:00:00Z",
            "retries": 99,
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["retries"] == 0
    # Internal fields are not part of the API contract at all.
    assert "claim_token" not in body
    assert "worker_id" not in body
    assert "lease_expires_at" not in body

    # And they were not persisted either.
    session = sessionmaker(bind=engine)()
    try:
        task = session.get(AiTask, body["id"])
        assert task.claim_token is None
        assert task.worker_id is None
        assert task.lease_expires_at is None
    finally:
        session.close()


# ---------------------------------------------------------------------------
# GET /api/v1/ai/tasks
# ---------------------------------------------------------------------------


def test_list_tasks_empty(ai_env):
    """Listing with no tasks returns an empty page with total 0."""
    client = ai_env["client"]
    response = client.get("/api/v1/ai/tasks")
    assert response.status_code == 200
    body = response.json()
    assert body == {"items": [], "total": 0, "limit": 50, "offset": 0}


def test_list_tasks_pagination_and_ordering(ai_env):
    """Pagination works and results are ordered created_at DESC, id DESC."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_file(engine)
    ids = [
        _create_task(client, record.id)["id"] for _ in range(5)
    ]

    response = client.get("/api/v1/ai/tasks", params={"limit": 2, "offset": 0})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert len(body["items"]) == 2

    # Stable ordering: the id tiebreaker makes the full order deterministic.
    page1_ids = [item["id"] for item in body["items"]]
    response = client.get("/api/v1/ai/tasks", params={"limit": 2, "offset": 2})
    page2_ids = [item["id"] for item in response.json()["items"]]
    response = client.get("/api/v1/ai/tasks", params={"limit": 2, "offset": 4})
    page3_ids = [item["id"] for item in response.json()["items"]]
    assert len(set(page1_ids + page2_ids + page3_ids)) == 5
    assert set(page1_ids + page2_ids + page3_ids) == set(ids)


def test_list_tasks_filter_by_file_id(ai_env):
    """file_id filter returns only tasks for that file."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record_a = _seed_file(engine, "a.txt")
    session = sessionmaker(bind=engine)()
    try:
        # Second file needs a distinct provider_file_id (unique constraint).
        record_b = UnifiedFileRecord(
            provider="LOCAL",
            provider_account_id=None,
            provider_file_id="seed_uuid_0002",
            name="b.txt",
            logical_path="/b.txt",
            mime_type="text/plain",
            size_bytes=11,
            sync_state="LOCAL",
        )
        session.add(record_b)
        session.commit()
        session.refresh(record_b)
    finally:
        session.close()

    _create_task(client, record_a.id)
    _create_task(client, record_a.id)
    _create_task(client, record_b.id)

    response = client.get(f"/api/v1/ai/tasks?file_id={record_a.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert all(item["file_id"] == record_a.id for item in body["items"])

    response = client.get(f"/api/v1/ai/tasks?file_id={record_b.id}")
    assert response.json()["total"] == 1

    # Unknown file_id yields an empty page (not an error).
    response = client.get("/api/v1/ai/tasks?file_id=file_never_created")
    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_list_tasks_filter_by_status(ai_env):
    """status filter returns only tasks in that state."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_file(engine)
    _create_task(client, record.id)
    _seed_task(engine, record.id, status="SUCCEEDED")

    response = client.get("/api/v1/ai/tasks?status=PENDING")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "PENDING"

    response = client.get("/api/v1/ai/tasks?status=SUCCEEDED")
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["status"] == "SUCCEEDED"

    # Combined filters.
    response = client.get(
        f"/api/v1/ai/tasks?file_id={record.id}&status=PENDING"
    )
    assert response.json()["total"] == 1


def test_list_tasks_invalid_pagination_returns_422(ai_env):
    """Pagination bounds are enforced (limit 1..100, offset >= 0)."""
    client = ai_env["client"]
    assert client.get("/api/v1/ai/tasks?limit=0").status_code == 422
    assert client.get("/api/v1/ai/tasks?limit=101").status_code == 422
    assert client.get("/api/v1/ai/tasks?offset=-1").status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/ai/tasks/{task_id}
# ---------------------------------------------------------------------------


def test_get_task_detail(ai_env):
    """Retrieving an existing task returns its full public fields."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_file(engine)
    created = _create_task(client, record.id)

    response = client.get(f"/api/v1/ai/tasks/{created['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == created["id"]
    assert body["file_id"] == record.id
    assert body["status"] == "PENDING"
    assert body["task_type"] == "text_classification"


def test_get_task_not_found_returns_404(ai_env):
    """Retrieving an unknown task id returns 404."""
    client = ai_env["client"]
    response = client.get("/api/v1/ai/tasks/task_does_not_exist_99999")
    assert response.status_code == 404
    assert "detail" in response.json()


def test_task_response_never_exposes_internal_fields(ai_env):
    """claim_token, worker_id, lease_expires_at and
    cancellation_requested_at must never appear in any API response, even
    when the row carries them (set by a future worker)."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_file(engine)
    task = _seed_task(
        engine,
        record.id,
        status="PROCESSING",
        claim_token="real_worker_token",
        worker_id="worker-1",
        started_at=datetime.now(timezone.utc),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        cancellation_requested_at=datetime.now(timezone.utc),
    )

    response = client.get(f"/api/v1/ai/tasks/{task.id}")
    assert response.status_code == 200
    body = response.json()
    assert "claim_token" not in body
    assert "worker_id" not in body
    assert "lease_expires_at" not in body
    assert "cancellation_requested_at" not in body

    # Same for the list endpoint.
    list_resp = client.get("/api/v1/ai/tasks")
    assert list_resp.status_code == 200
    for item in list_resp.json()["items"]:
        assert "claim_token" not in item
        assert "worker_id" not in item
        assert "lease_expires_at" not in item
        assert "cancellation_requested_at" not in item


# ---------------------------------------------------------------------------
# DELETE /api/v1/ai/tasks/{task_id}
# ---------------------------------------------------------------------------


def test_cancel_pending_task_succeeds(ai_env):
    """Cancelling a PENDING task transitions it to CANCELLED with completed_at."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_file(engine)
    created = _create_task(client, record.id)

    response = client.delete(f"/api/v1/ai/tasks/{created['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "CANCELLED"
    assert body["completed_at"] is not None

    # The row still exists (history is preserved; nothing is deleted).
    detail = client.get(f"/api/v1/ai/tasks/{created['id']}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "CANCELLED"


def test_cancel_pending_task_is_idempotent(ai_env):
    """Repeatedly cancelling an already CANCELLED task returns 200 and
    changes nothing (idempotent)."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_file(engine)
    created = _create_task(client, record.id)

    first = client.delete(f"/api/v1/ai/tasks/{created['id']}")
    assert first.status_code == 200
    completed_at = first.json()["completed_at"]

    # Repeat several times.
    for _ in range(3):
        again = client.delete(f"/api/v1/ai/tasks/{created['id']}")
        assert again.status_code == 200
        assert again.json()["status"] == "CANCELLED"
        # The original completed_at is preserved (no re-stamping).
        assert again.json()["completed_at"] == completed_at


def test_cancel_processing_task_requests_cancellation(ai_env):
    """Cancelling a PROCESSING task only sets cancellation_requested_at; the
    status is left unchanged because only the worker can finalize it."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_file(engine)
    task = _seed_task(
        engine,
        record.id,
        status="PROCESSING",
        claim_token="worker_token_1",
        worker_id="worker-1",
        started_at=datetime.now(timezone.utc),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    assert task.cancellation_requested_at is None

    response = client.delete(f"/api/v1/ai/tasks/{task.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "PROCESSING"  # status NOT changed by the API
    assert body["completed_at"] is None

    # The request marker was persisted; claim fields are untouched.
    session = sessionmaker(bind=engine)()
    try:
        fresh = session.get(AiTask, task.id)
        assert fresh.status == "PROCESSING"
        assert fresh.cancellation_requested_at is not None
        assert fresh.claim_token == "worker_token_1"
        assert fresh.worker_id == "worker-1"
        assert fresh.lease_expires_at is not None
    finally:
        session.close()


def test_cancel_processing_task_request_is_idempotent(ai_env):
    """A second cancellation request for the same PROCESSING task does not
    overwrite the first cancellation_requested_at timestamp."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_file(engine)
    task = _seed_task(
        engine,
        record.id,
        status="PROCESSING",
        claim_token="worker_token_2",
        worker_id="worker-2",
    )

    first = client.delete(f"/api/v1/ai/tasks/{task.id}")
    assert first.status_code == 200
    session = sessionmaker(bind=engine)()
    try:
        first_ts = session.get(AiTask, task.id).cancellation_requested_at
    finally:
        session.close()
    assert first_ts is not None

    second = client.delete(f"/api/v1/ai/tasks/{task.id}")
    assert second.status_code == 200
    assert second.json()["status"] == "PROCESSING"
    session = sessionmaker(bind=engine)()
    try:
        second_ts = session.get(AiTask, task.id).cancellation_requested_at
    finally:
        session.close()
    assert second_ts == first_ts


def test_cancel_terminal_task_returns_409(ai_env):
    """Cancelling a SUCCEEDED or FAILED task returns 409 and changes nothing."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_file(engine)
    succeeded = _seed_task(engine, record.id, status="SUCCEEDED")
    failed = _seed_task(engine, record.id, status="FAILED", error_message="boom")

    for task in (succeeded, failed):
        response = client.delete(f"/api/v1/ai/tasks/{task.id}")
        assert response.status_code == 409
        assert "detail" in response.json()

    # Rows are untouched.
    assert client.get(f"/api/v1/ai/tasks/{succeeded.id}").json()["status"] == "SUCCEEDED"
    assert client.get(f"/api/v1/ai/tasks/{failed.id}").json()["status"] == "FAILED"


def test_cancel_task_not_found_returns_404(ai_env):
    """Cancelling an unknown task id returns 404."""
    client = ai_env["client"]
    response = client.delete("/api/v1/ai/tasks/task_does_not_exist_99999")
    assert response.status_code == 404
    assert "detail" in response.json()


def test_cancel_does_not_touch_file_metadata(ai_env):
    """Cancelling a task must not modify the original file metadata."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_file(engine)
    before = client.get(f"/api/v1/files/{record.id}").json()

    created = _create_task(client, record.id)
    client.delete(f"/api/v1/ai/tasks/{created['id']}")

    after = client.get(f"/api/v1/files/{record.id}").json()
    assert before == after


def test_cancel_race_with_worker_claim(ai_env):
    """If a worker claims the task between the API's read and its guarded
    UPDATE, the API must not lose the cancellation: it falls back to
    setting cancellation_requested_at on the now-PROCESSING task."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_file(engine)
    created = _create_task(client, record.id)

    # Simulate a worker claiming the PENDING task (atomic claim as the
    # future worker design specifies).
    session = sessionmaker(bind=engine)()
    try:
        now = datetime.now(timezone.utc)
        task = session.get(AiTask, created["id"])
        task.status = "PROCESSING"
        task.claim_token = "race_worker"
        task.worker_id = "worker-race"
        task.started_at = now
        task.lease_expires_at = now + timedelta(minutes=5)
        session.commit()
    finally:
        session.close()

    response = client.delete(f"/api/v1/ai/tasks/{created['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "PROCESSING"

    session = sessionmaker(bind=engine)()
    try:
        fresh = session.get(AiTask, created["id"])
        assert fresh.cancellation_requested_at is not None
        assert fresh.claim_token == "race_worker"
    finally:
        session.close()
