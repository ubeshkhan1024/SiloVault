"""End-to-end verification of the image description and approval workflow.

M5.1 Step 4.  The workflow under test is the existing production composition:

    API create -> AiWorkerService claim -> ImageDescriptionProcessor
    -> LocalAdapter download -> AiExecutionService -> vision runtime
    -> proposal stored -> AWAITING_APPROVAL -> approve / reject

Only the vision runtime is simulated (:class:`FakeVisionRuntime`).  Everything
else is real: the SQLite schema and sessions, the storage adapter and its
registry, the worker (claim, lease, ownership release), the execution bridge
(including its Pillow-backed image validation), the processor, the approval
service, and the FastAPI endpoints.

No model is downloaded and no network access occurs.  Every file and the
database live under ``tmp_path``, so real user files and cloud providers are
never touched.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.ai import AiModelServices, ModelDescriptor, ModelManager, Profile, build_ai_worker
from backend.ai.execution import IMAGE_DESCRIPTION_CAPABILITY
from backend.ai.interfaces import ImageDescriptionResult
from backend.ai.storage import ModelStorage
from backend.config import settings
from backend.database import Base, get_db
from backend.main import app
from backend.models.ai_task import AiTask
from backend.models.unified_file import UnifiedFileRecord
from backend.storage.capabilities import CAPABILITY_MAP
from backend.storage.local import LocalAdapter
from backend.storage.registry import register as registry_register
from backend.storage.registry import remove as registry_remove

#: Isolated provider id: the workflow resolves the adapter through the real
#: storage registry instead of having one injected.
TEST_PROVIDER = "TEST_E2E_IMAGE_PROVIDER"

API_TASKS = "/api/v1/ai/tasks"
TASK_TYPE = "image_description"
MODEL_ID = "vendor.test.vision"
WORKER_ID = "e2e-worker-1"


# ---------------------------------------------------------------------------
# Deterministic image bytes
# ---------------------------------------------------------------------------


def _png_bytes(size=(8, 8), colour=(200, 120, 40)) -> bytes:
    """Return a small, valid PNG built by Pillow (a declared dependency)."""
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Fake vision runtime
# ---------------------------------------------------------------------------


class FakeVisionRuntime:
    """Deterministic in-process stand-in for a local vision model.

    It implements the structural ``ImageDescriptionRuntime`` protocol:
    ``name``, ``version``, ``is_available()``, ``supported_modalities()`` and
    ``describe_image()``.  No model is loaded and no inference happens.
    """

    def __init__(self, *, result=None, available=True, observer=None):
        self.name = "fake-vision-runtime"
        self.version = "1.0"
        self._result = (
            result
            if result is not None
            else ImageDescriptionResult(
                description="A small orange square on a plain background.",
                tags=("orange", "square", "test-image"),
                model=None,  # exercise the descriptor-identifier fallback
                confidence=0.87,
            )
        )
        self.available = available
        self.observer = observer
        self.calls = []

    def is_available(self) -> bool:
        return self.available

    def supported_modalities(self):
        return ["vision"]

    def describe_image(self, image, mime_type=None, **kwargs):
        self.calls.append(
            {
                "image": bytes(image),
                "mime_type": mime_type,
                "observed": self.observer() if self.observer is not None else None,
            }
        )
        return self._result


# ---------------------------------------------------------------------------
# Recording storage adapter (real LocalAdapter behavior, audited)
# ---------------------------------------------------------------------------


class RecordingLocalAdapter(LocalAdapter):
    """Real local storage that records downloads and forbids mutation."""

    def __init__(self, storage_root):
        super().__init__(storage_root)
        self.calls = []

    def download(self, provider_file_id):
        self.calls.append(("download", provider_file_id))
        return super().download(provider_file_id)

    def rename(self, provider_file_id, new_name):  # pragma: no cover - forbidden
        self.calls.append(("rename", provider_file_id))
        raise AssertionError("the AI workflow must not rename stored files")

    def move(self, source_provider_file_id, target_logical_path):  # pragma: no cover
        self.calls.append(("move", source_provider_file_id))
        raise AssertionError("the AI workflow must not move stored files")

    def quarantine(self, provider_file_id):  # pragma: no cover - forbidden
        self.calls.append(("quarantine", provider_file_id))
        raise AssertionError("the AI workflow must not quarantine files")

    def restore(self, quarantine_id):  # pragma: no cover - forbidden
        self.calls.append(("restore", quarantine_id))
        raise AssertionError("the AI workflow must not restore files")

    def cleanup(self, quarantine_id):  # pragma: no cover - forbidden
        self.calls.append(("cleanup", quarantine_id))
        raise AssertionError("the AI workflow must not clean up files")

    @property
    def download_calls(self):
        return [call for call in self.calls if call[0] == "download"]


# ---------------------------------------------------------------------------
# Model services and worker composition
# ---------------------------------------------------------------------------


def _vision_services(models_root, runtime):
    """Build AiModelServices with image_description executable via *runtime*."""
    manager = ModelManager()
    manager.set_profile(Profile.BALANCED)
    descriptor = ModelDescriptor(
        name="Test Vision Descriptor",
        version="1.0",
        modalities=("vision",),
        model_id=MODEL_ID,
    )
    manager.register_capability(
        IMAGE_DESCRIPTION_CAPABILITY, descriptor, Profile.BALANCED
    )
    manager.register_runtime(runtime)
    manager.associate_runtime_with_model(runtime.name, descriptor.identifier)
    services = AiModelServices(
        storage=ModelStorage(root=models_root),
        registry=manager.model_registry,
        manager=manager,
    )
    return services, descriptor


def _worker_for(env, runtime):
    """Build the real worker registered with a processor for *runtime*."""
    services, _descriptor = _vision_services(env["models_root"], runtime)
    return build_ai_worker(lambda: env["Session"](), services)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workflow_env(tmp_path, monkeypatch):
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
    monkeypatch.setattr(settings, "uploads_dir", str(uploads_dir))

    db_path = (tmp_path / "workflow.sqlite").as_posix()
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        future=True,
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

    storage_root = tmp_path / "blobs"
    adapter = RecordingLocalAdapter(storage_root)
    registry_register(TEST_PROVIDER, adapter, CAPABILITY_MAP["LOCAL"])

    runtime = FakeVisionRuntime()
    services, descriptor = _vision_services(tmp_path / "models", runtime)
    worker = build_ai_worker(lambda: TestSession(), services)

    try:
        yield {
            "client": TestClient(app),
            "engine": engine,
            "Session": TestSession,
            "adapter": adapter,
            "storage_root": storage_root,
            "runtime": runtime,
            "services": services,
            "descriptor": descriptor,
            "worker": worker,
            "models_root": tmp_path / "models",
        }
    finally:
        registry_remove(TEST_PROVIDER)
        app.dependency_overrides.clear()
        engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_image(env, content, *, name="photo.png", mime_type="image/png"):
    """Store *content* through the adapter and seed its UnifiedFileRecord."""
    stored = env["adapter"].upload(content, name, f"/{name}")

    session = env["Session"]()
    try:
        record = UnifiedFileRecord(
            provider=TEST_PROVIDER,
            provider_account_id=None,
            provider_file_id=stored.provider_file_id,
            name=name,
            logical_path=f"/{name}",
            mime_type=mime_type,
            size_bytes=len(content),
            sync_state="LOCAL",
        )
        session.add(record)
        session.commit()
        return record.id, stored.provider_file_id
    finally:
        session.close()


def _seed_record_without_source(env, *, name="missing.png", size_bytes=64):
    """Seed a record whose provider file id does not exist in storage."""
    session = env["Session"]()
    try:
        record = UnifiedFileRecord(
            provider=TEST_PROVIDER,
            provider_account_id=None,
            provider_file_id="missing_source_file_0001",
            name=name,
            logical_path=f"/{name}",
            mime_type="image/png",
            size_bytes=size_bytes,
            sync_state="LOCAL",
        )
        session.add(record)
        session.commit()
        return record.id
    finally:
        session.close()


def _create_task(client, file_id):
    response = client.post(API_TASKS, json={"file_id": file_id, "task_type": TASK_TYPE})
    assert response.status_code == 201, response.text
    return response.json()


def _get_task(env, task_id):
    session = env["Session"]()
    try:
        task = session.get(AiTask, task_id)
        assert task is not None
        return task
    finally:
        session.close()


def _file_snapshot(env, file_id):
    session = env["Session"]()
    try:
        record = session.get(UnifiedFileRecord, file_id)
        assert record is not None
        return {
            "provider": record.provider,
            "provider_file_id": record.provider_file_id,
            "name": record.name,
            "logical_path": record.logical_path,
            "mime_type": record.mime_type,
            "size_bytes": record.size_bytes,
            "sha256": record.sha256,
            "category": record.category,
            "tags": list(record.tags),
            "sync_state": record.sync_state,
            "provider_metadata": dict(record.provider_metadata or {}),
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }
    finally:
        session.close()


def _stored_files(env):
    return sorted(p.name for p in env["storage_root"].iterdir() if p.is_file())


def _observe_processing_task(env, sink):
    """Record the task's ownership fields at the moment inference is attempted."""
    session = env["Session"]()
    try:
        task = session.query(AiTask).filter(AiTask.status == "PROCESSING").one()
        sink.update(
            {
                "status": task.status,
                "worker_id": task.worker_id,
                "claim_token": task.claim_token,
                "lease_expires_at": task.lease_expires_at,
                "proposal_json": task.proposal_json,
            }
        )
        return dict(sink)
    finally:
        session.close()


def _expected_proposal(env, task_id, file_id, provider_file_id, content, understanding):
    return {
        "task_id": task_id,
        "task_type": TASK_TYPE,
        "file": {
            "id": file_id,
            "provider": TEST_PROVIDER,
            "provider_file_id": provider_file_id,
            "name": "photo.png",
            "mime_type": "image/png",
            "size_bytes": len(content),
        },
        "image_understanding": understanding,
    }


# ---------------------------------------------------------------------------
# Full workflow
# ---------------------------------------------------------------------------


def test_full_workflow_stores_approval_ready_proposal(workflow_env):
    """Create -> claim -> download -> infer -> proposal -> AWAITING_APPROVAL."""
    env = workflow_env
    png = _png_bytes()
    file_id, provider_file_id = _seed_image(env, png)
    env["adapter"].calls.clear()

    created = _create_task(env["client"], file_id)
    assert created["status"] == "PENDING"
    assert created["task_type"] == TASK_TYPE
    assert created["proposal_json"] is None
    assert created["started_at"] is None

    observed = {}
    runtime = FakeVisionRuntime(observer=lambda: _observe_processing_task(env, observed))
    worker = _worker_for(env, runtime)

    assert worker.process_one_task(WORKER_ID, TASK_TYPE) is True

    # 1. The image was fetched through the storage adapter, exactly once.
    assert env["adapter"].download_calls == [("download", provider_file_id)]

    # 2. The runtime received the stored bytes and the record's mime type.
    assert len(runtime.calls) == 1
    assert runtime.calls[0]["image"] == png
    assert runtime.calls[0]["mime_type"] == "image/png"

    # 3. Ownership was held during inference.
    assert observed["status"] == "PROCESSING"
    assert observed["worker_id"] == WORKER_ID
    assert observed["claim_token"]
    assert observed["lease_expires_at"] > datetime.now(timezone.utc)
    assert observed["proposal_json"] is None

    # 4. The task transitioned to approval with a persisted proposal.
    task = _get_task(env, created["id"])
    assert task.status == "AWAITING_APPROVAL"
    assert task.proposal_json == _expected_proposal(
        env,
        created["id"],
        file_id,
        provider_file_id,
        png,
        {
            "description": "A small orange square on a plain background.",
            "tags": ["orange", "square", "test-image"],
            "model": MODEL_ID,  # runtime supplied no model, descriptor used
            "confidence": 0.87,
        },
    )
    assert task.decision is None
    assert task.decided_at is None
    assert task.error_message is None
    assert task.retries == 0

    # 5. Ownership is released and the lifecycle timestamps are set.
    assert task.started_at is not None
    assert task.completed_at is not None
    assert task.claim_token is None
    assert task.worker_id is None
    assert task.lease_expires_at is None

    # 6. The API exposes the same approval-ready state.
    detail = env["client"].get(f"{API_TASKS}/{created['id']}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "AWAITING_APPROVAL"
    assert detail.json()["proposal_json"] == task.proposal_json


def test_confidence_is_omitted_when_runtime_does_not_supply_it(workflow_env):
    env = workflow_env
    png = _png_bytes()
    file_id, provider_file_id = _seed_image(env, png)

    runtime = FakeVisionRuntime(
        result=ImageDescriptionResult(
            description="A cat.",
            tags=("cat",),
            model="fake-vision-local",
            confidence=None,
        )
    )
    worker = _worker_for(env, runtime)
    created = _create_task(env["client"], file_id)

    assert worker.process_one_task(WORKER_ID, TASK_TYPE) is True

    task = _get_task(env, created["id"])
    understanding = task.proposal_json["image_understanding"]
    assert understanding == {
        "description": "A cat.",
        "tags": ["cat"],
        "model": "fake-vision-local",
    }
    assert "confidence" not in understanding
    # The stored JSON contains no fabricated confidence key either.
    assert "confidence" not in env["client"].get(
        f"{API_TASKS}/{created['id']}"
    ).json()["proposal_json"]["image_understanding"]


# ---------------------------------------------------------------------------
# Human decision paths
# ---------------------------------------------------------------------------


def _run_workflow_to_approval(env):
    png = _png_bytes()
    file_id, provider_file_id = _seed_image(env, png)
    created = _create_task(env["client"], file_id)
    assert env["worker"].process_one_task(WORKER_ID, TASK_TYPE) is True
    return png, file_id, provider_file_id, created["id"]


def test_approve_records_decision_without_touching_file_or_image(workflow_env):
    env = workflow_env
    env["adapter"].calls.clear()
    png = _png_bytes()
    file_id, provider_file_id = _seed_image(env, png)
    created = _create_task(env["client"], file_id)

    before_record = _file_snapshot(env, file_id)
    before_files = _stored_files(env)
    env["adapter"].calls.clear()

    assert env["worker"].process_one_task(WORKER_ID, TASK_TYPE) is True
    awaiting = _get_task(env, created["id"])
    proposal = awaiting.proposal_json
    assert awaiting.status == "AWAITING_APPROVAL"

    response = env["client"].post(f"{API_TASKS}/{created['id']}/approve")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "SUCCEEDED"
    assert body["decision"] == "APPROVE"
    assert body["decided_at"] is not None
    assert body["completed_at"] is not None
    assert body["proposal_json"] == proposal

    task = _get_task(env, created["id"])
    assert task.status == "SUCCEEDED"
    assert task.decision == "APPROVE"
    assert task.decided_at is not None
    assert task.proposal_json == proposal

    # The original file record and the stored image are untouched.
    assert _file_snapshot(env, file_id) == before_record
    assert (env["storage_root"] / provider_file_id).read_bytes() == png
    assert _stored_files(env) == before_files
    # Only the worker's download occurred; approval performed no storage I/O.
    assert env["adapter"].calls == [("download", provider_file_id)]


def test_reject_records_decision_without_touching_file_or_image(workflow_env):
    env = workflow_env
    png = _png_bytes()
    file_id, provider_file_id = _seed_image(env, png)
    created = _create_task(env["client"], file_id)

    before_record = _file_snapshot(env, file_id)
    before_files = _stored_files(env)
    env["adapter"].calls.clear()

    assert env["worker"].process_one_task(WORKER_ID, TASK_TYPE) is True
    awaiting = _get_task(env, created["id"])
    proposal = awaiting.proposal_json
    assert awaiting.status == "AWAITING_APPROVAL"

    response = env["client"].post(f"{API_TASKS}/{created['id']}/reject")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "REJECTED"
    assert body["decision"] == "REJECT"
    assert body["decided_at"] is not None
    assert body["completed_at"] is not None
    assert body["proposal_json"] == proposal

    task = _get_task(env, created["id"])
    assert task.status == "REJECTED"
    assert task.decision == "REJECT"
    assert task.decided_at is not None
    assert task.proposal_json == proposal

    assert _file_snapshot(env, file_id) == before_record
    assert (env["storage_root"] / provider_file_id).read_bytes() == png
    assert _stored_files(env) == before_files
    assert env["adapter"].calls == [("download", provider_file_id)]


# ---------------------------------------------------------------------------
# Failure coverage
# ---------------------------------------------------------------------------


def test_corrupt_image_fails_task_without_proposal(workflow_env):
    env = workflow_env
    corrupt = b"\x89PNG\r\n\x1a\nthis is not a decodable png"
    file_id, provider_file_id = _seed_image(env, corrupt)
    env["adapter"].calls.clear()

    created = _create_task(env["client"], file_id)
    assert env["worker"].process_one_task(WORKER_ID, TASK_TYPE) is True

    task = _get_task(env, created["id"])
    assert task.status == "FAILED"
    assert task.proposal_json is None
    assert task.error_message
    assert "corrupt" in task.error_message.lower()
    assert task.completed_at is not None
    assert task.claim_token is None
    assert task.worker_id is None
    assert task.lease_expires_at is None
    # Download was attempted through storage; inference was never reached.
    assert env["adapter"].download_calls == [("download", provider_file_id)]
    assert env["runtime"].calls == []


def test_missing_source_file_fails_task_before_inference(workflow_env):
    env = workflow_env
    file_id = _seed_record_without_source(env)
    env["adapter"].calls.clear()

    created = _create_task(env["client"], file_id)
    assert env["worker"].process_one_task(WORKER_ID, TASK_TYPE) is True

    task = _get_task(env, created["id"])
    assert task.status == "FAILED"
    assert task.proposal_json is None
    assert task.error_message
    assert "not found" in task.error_message.lower()
    assert env["adapter"].download_calls == [
        ("download", "missing_source_file_0001")
    ]
    assert env["runtime"].calls == []


def test_unavailable_runtime_keeps_task_pending(workflow_env):
    """No executable vision runtime means no processor and no claim."""
    env = workflow_env
    png = _png_bytes()
    file_id, _provider_file_id = _seed_image(env, png)
    env["adapter"].calls.clear()

    runtime = FakeVisionRuntime(available=False)
    worker = _worker_for(env, runtime)
    assert worker.get_registered_task_types() == []

    created = _create_task(env["client"], file_id)
    assert worker.process_one_task(WORKER_ID, TASK_TYPE) is False

    task = _get_task(env, created["id"])
    assert task.status == "PENDING"
    assert task.claim_token is None
    assert task.worker_id is None
    assert task.lease_expires_at is None
    assert task.proposal_json is None
    assert env["adapter"].download_calls == []
    assert runtime.calls == []


def test_runtime_unavailable_at_inference_time_fails_task(workflow_env):
    """A runtime that becomes unavailable after registration fails the task."""
    env = workflow_env
    png = _png_bytes()
    file_id, provider_file_id = _seed_image(env, png)
    env["adapter"].calls.clear()

    runtime = FakeVisionRuntime()
    worker = _worker_for(env, runtime)
    created = _create_task(env["client"], file_id)

    runtime.available = False

    assert worker.process_one_task(WORKER_ID, TASK_TYPE) is True

    task = _get_task(env, created["id"])
    assert task.status == "FAILED"
    assert task.proposal_json is None
    assert "unavailable" in task.error_message.lower()
    # The image was downloaded through storage before the runtime was probed.
    assert env["adapter"].download_calls == [("download", provider_file_id)]
    assert runtime.calls == []
