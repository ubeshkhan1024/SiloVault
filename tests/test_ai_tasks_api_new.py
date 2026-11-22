"""Tests for the AI task API endpoints (M4.1 Stage 1B.1).

Covers POST/GET/DELETE on /api/v1/ai/tasks using an isolated
in-memory SQLite database (StaticPool) and the real FastAPI app with
get_db overridden — the same fixture pattern as
tests/test_file_rename_move.py.

No worker, no inference, no Ollama, no model downloads: the tests only
exercise the API layer over the durable AiTask model. The development
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

# (existing tests omitted for brevity - see original test_ai_tasks_api.py)


# ---------------------------------------------------------------------------
# POST /api/v1/ai/tasks/{task_id}/approve
# ---------------------------------------------------------------------------


def test_approve_task_success(ai_env):
    """Approving an AWAITING_APPROVAL task transitions to SUCCEEDED."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    from sqlalchemy.orm import sessionmaker

    record = _seed_file(engine)
    session = sessionmaker(bind=engine)()
    try:
        task = _seed_task(
            engine,
            record.id,
            status="AWAITING_APPROVAL",
            proposal_json={"category": "document", "confidence": 0.95},
        )
    finally:
        session.close()

    response = client.post(f"/api/v1/ai/tasks/{task.id}/approve")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "SUCCEEDED"
    assert body["decision"] == "APPROVE"
    assert body["decided_at"] is not None
    assert body["completed_at"] is not None
    assert "claim_token" not in body
    assert "worker_id" not in body
    assert "lease_expires_at" not in body
    assert body["proposal_json"] == {"category": "document", "confidence": 0.95}


def test_approve_task_not_found_returns_404(ai_env):
    """Approving a non-existent task returns 404."""
    env = ai_env
    client = env["client"]

    response = client.post("/api/v1/ai/tasks/nonexistent_id/approve")
    assert response.status_code == 404
    assert "detail" in response.json()


def test_approve_task_wrong_status_returns_409(ai_env):
    """Approving a task not in AWAITING_APPROVAL returns 409."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_file(engine)
    task = _seed_task(engine, record.id, status="PROCESSING")

    response = client.post(f"/api/v1/ai/tasks/{task.id}/approve")
    assert response.status_code == 409
    assert "detail" in response.json()


def test_approve_duplicate_decision_returns_409(ai_env):
    """Approving an already-approved task returns 409."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_file(engine)
    task = _seed_task(
        engine,
        record.id,
        status="AWAITING_APPROVAL",
    )

    # First approval succeeds
    first = client.post(f"/api/v1/ai/tasks/{task.id}/approve")
    assert first.status_code == 200
    assert first.json()["status"] == "SUCCEEDED"

    # Second approval fails
    second = client.post(f"/api/v1/ai/tasks/{task.id}/approve")
    assert second.status_code == 409
    assert "detail" in second.json()

    # Task remains SUCCEEDED
    detail = client.get(f"/api/v1/ai/tasks/{task.id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "SUCCEEDED"


# ---------------------------------------------------------------------------
# POST /api/v1/ai/tasks/{task_id}/reject
# ---------------------------------------------------------------------------


def test_reject_task_success(ai_env):
    """Rejecting an AWAITING_APPROVAL task transitions to REJECTED."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    from sqlalchemy.orm import sessionmaker

    record = _seed_file(engine)
    # Seed task in AWAITING_APPROVAL state
    session = sessionmaker(bind=engine)()
    try:
        task = _seed_task(
            engine,
            record.id,
            status="AWAITING_APPROVAL",
            proposal_json={"category": "document", "confidence": 0.95},
        )
    finally:
        session.close()

    response = client.post(f"/api/v1/ai/tasks/{task.id}/reject")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "REJECTED"
    assert body["decision"] == "REJECT"
    assert body["decided_at"] is not None
    assert body["completed_at"] is not None
    assert "claim_token" not in body
    assert "worker_id" not in body
    assert "lease_expires_at" not in body
    assert body["proposal_json"] == {"category": "document", "confidence": 0.95}


def test_reject_task_not_found_returns_404(ai_env):
    """Rejecting a non-existent task returns 404."""
    env = ai_env
    client = env["client"]

    response = client.post("/api/v1/ai/tasks/nonexistent_id/reject")
    assert response.status_code == 404
    assert "detail" in response.json()


def test_reject_task_wrong_status_returns_409(ai_env):
    """Rejecting a task not in AWAITING_APPROVAL returns 409."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_file(engine)
    task = _seed_task(engine, record.id, status="PROCESSING")

    response = client.post(f"/api/v1/ai/tasks/{task.id}/reject")
    assert response.status_code == 409
    assert "detail" in response.json()


def test_reject_duplicate_decision_returns_409(ai_env):
    """Rejecting an already-rejected task returns 409."""
    env = ai_env
    client = env["client"]
    engine = env["engine"]

    record = _seed_file(engine)
    task = _seed_task(
        engine,
        record.id,
        status="AWAITING_APPROVAL",
    )

    # First rejection succeeds
    first = client.post(f"/api/v1/ai/tasks/{task.id}/reject")
    assert first.status_code == 200
    assert first.json()["status"] == "REJECTED"

    # Second rejection fails
    second = client.post(f"/api/v1/ai/tasks/{task.id}/reject")
    assert second.status_code == 409
    assert "detail" in second.json()

    # Task remains REJECTED
    detail = client.get(f"/api/v1/ai/tasks/{task.id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "REJECTED"