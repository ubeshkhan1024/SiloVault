"""Focused tests for processor registration with the AI worker (M4.4 Step 3C)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.ai import (
    AiModelServices,
    ModelDescriptor,
    ModelManager,
    Profile,
    build_ai_worker,
    build_task_processors,
    register_task_processors,
)
from backend.ai.execution import TEXT_CLASSIFICATION_CAPABILITY
from backend.ai.processors import (
    TEXT_CLASSIFICATION_TASK_TYPE,
    TextClassificationProcessor,
)
from backend.ai.storage import ModelStorage
from backend.database import Base
from backend.models.ai_task import AiTask
from backend.models.unified_file import UnifiedFileRecord
from backend.services.ai_worker import AiWorkerService
from backend.storage import registry as storage_registry
from backend.storage.capabilities import CAPABILITY_MAP
from backend.storage.interfaces import StorageOperations

TEST_PROVIDER = "TEST_REGISTRATION_PROVIDER"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeClassificationRuntime:
    name = "registration-classifier"
    version = "1.0"

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def is_available(self):
        return True

    def supported_modalities(self):
        return ["text"]

    def classify(self, image=None, text=None):
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return {"document": 0.9, "receipt": 0.1}


class FakeStorageAdapter(StorageOperations):
    def __init__(self, content=b"hello world\n"):
        self.content = content
        self.calls = []

    def download(self, provider_file_id):
        self.calls.append(("download", provider_file_id))
        return self.content

    def upload(self, file_data, name, logical_path):  # pragma: no cover - forbidden
        raise AssertionError("processor must not upload")

    def rename(self, provider_file_id, new_name):  # pragma: no cover - forbidden
        raise AssertionError("processor must not rename")

    def move(self, source_provider_file_id, target_logical_path):  # pragma: no cover
        raise AssertionError("processor must not move")

    def quarantine(self, provider_file_id):  # pragma: no cover - forbidden
        raise AssertionError("processor must not quarantine")

    def restore(self, quarantine_id):  # pragma: no cover - forbidden
        raise AssertionError("processor must not restore")

    def cleanup(self, quarantine_id):  # pragma: no cover - forbidden
        raise AssertionError("processor must not cleanup")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ai_registration_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield TestSession
    finally:
        engine.dispose()


def _services(tmp_path, runtime=None, *, executable=True):
    """Build AiModelServices for an executable or empty model configuration."""
    manager = ModelManager()
    runtime = runtime if runtime is not None else FakeClassificationRuntime()
    if executable:
        descriptor = ModelDescriptor(
            name="Registration Classifier",
            version="1.0",
            modalities=("text",),
            model_id="vendor.registration.classifier",
        )
        manager.register_capability(
            TEXT_CLASSIFICATION_CAPABILITY, descriptor, Profile.EFFICIENT
        )
        manager.register_runtime(runtime)
        manager.associate_runtime_with_model(
            runtime.name, descriptor.identifier
        )
    return AiModelServices(
        storage=ModelStorage(root=tmp_path),
        registry=manager.model_registry,
        manager=manager,
    )


@pytest.fixture
def local_storage_adapter():
    adapter = FakeStorageAdapter()
    storage_registry.register(
        TEST_PROVIDER, adapter, CAPABILITY_MAP["LOCAL"]
    )
    try:
        yield adapter
    finally:
        storage_registry.remove(TEST_PROVIDER)


def _seed_task(Session, *, provider=TEST_PROVIDER, status="PENDING"):
    db = Session()
    try:
        record = UnifiedFileRecord(
            provider=provider,
            provider_account_id=None,
            provider_file_id="provider-file-1",
            name="notes.txt",
            logical_path="/notes.txt",
            mime_type="text/plain",
            size_bytes=12,
        )
        db.add(record)
        db.flush()
        task = AiTask(
            task_type=TEXT_CLASSIFICATION_TASK_TYPE,
            status=status,
            file_id=record.id,
        )
        db.add(task)
        db.commit()
        return task.id, record.id
    finally:
        db.close()


def _get_task(Session, task_id):
    db = Session()
    try:
        task = db.get(AiTask, task_id)
        assert task is not None
        return task
    finally:
        db.close()


def _empty_worker(Session):
    return AiWorkerService(lambda: Session())


# ---------------------------------------------------------------------------
# Registry resolution and duplicate handling
# ---------------------------------------------------------------------------


def test_get_processor_resolves_registered_task_type(ai_registration_db):
    worker = _empty_worker(ai_registration_db)
    processor = lambda task_id: {}

    worker.register_processor(TEXT_CLASSIFICATION_TASK_TYPE, processor)

    assert worker.get_processor(TEXT_CLASSIFICATION_TASK_TYPE) is processor


def test_get_processor_returns_none_for_unknown_task_type(ai_registration_db):
    worker = _empty_worker(ai_registration_db)
    assert worker.get_processor("metadata_extraction") is None


@pytest.mark.parametrize("task_type", ["", "   ", None, 42])
def test_get_processor_validates_task_type(ai_registration_db, task_type):
    worker = _empty_worker(ai_registration_db)
    with pytest.raises(ValueError, match="task_type must be a non-empty string"):
        worker.get_processor(task_type)


def test_identical_duplicate_registration_is_idempotent(ai_registration_db):
    worker = _empty_worker(ai_registration_db)
    processor = lambda task_id: {}

    worker.register_processor(TEXT_CLASSIFICATION_TASK_TYPE, processor)
    worker.register_processor(TEXT_CLASSIFICATION_TASK_TYPE, processor)

    assert worker.get_processor(TEXT_CLASSIFICATION_TASK_TYPE) is processor
    assert worker.get_registered_task_types() == [TEXT_CLASSIFICATION_TASK_TYPE]


def test_conflicting_duplicate_registration_is_rejected(ai_registration_db):
    worker = _empty_worker(ai_registration_db)
    original = lambda task_id: {"source": "original"}
    replacement = lambda task_id: {"source": "replacement"}

    worker.register_processor(TEXT_CLASSIFICATION_TASK_TYPE, original)
    with pytest.raises(ValueError, match="different processor is already registered"):
        worker.register_processor(TEXT_CLASSIFICATION_TASK_TYPE, replacement)

    assert worker.get_processor(TEXT_CLASSIFICATION_TASK_TYPE) is original


def test_unregister_allows_a_new_processor(ai_registration_db):
    worker = _empty_worker(ai_registration_db)
    first = lambda task_id: {}
    second = lambda task_id: {}

    worker.register_processor(TEXT_CLASSIFICATION_TASK_TYPE, first)
    worker.unregister_processor(TEXT_CLASSIFICATION_TASK_TYPE)
    worker.register_processor(TEXT_CLASSIFICATION_TASK_TYPE, second)

    assert worker.get_processor(TEXT_CLASSIFICATION_TASK_TYPE) is second


# ---------------------------------------------------------------------------
# Building processors from AI model services
# ---------------------------------------------------------------------------


def test_build_task_processors_returns_supported_type(tmp_path):
    services = _services(tmp_path)
    factory = lambda: None

    processors = build_task_processors(factory, services)

    assert set(processors) == {TEXT_CLASSIFICATION_TASK_TYPE}
    processor = processors[TEXT_CLASSIFICATION_TASK_TYPE]
    assert isinstance(processor, TextClassificationProcessor)
    assert callable(processor)


def test_build_task_processors_skips_unavailable_capability(tmp_path):
    services = _services(tmp_path, executable=False)
    assert services.manager.can_execute_capability(
        TEXT_CLASSIFICATION_CAPABILITY
    ) is False

    assert build_task_processors(lambda: None, services) == {}


def test_build_task_processors_rejects_invalid_dependencies(tmp_path):
    services = _services(tmp_path)

    with pytest.raises(TypeError, match="session_factory must be callable"):
        build_task_processors(None, services)

    with pytest.raises(TypeError, match="AiModelServices"):
        build_task_processors(lambda: None, object())


# ---------------------------------------------------------------------------
# Registering processors on a worker
# ---------------------------------------------------------------------------


def test_register_task_processors_registers_each_type(ai_registration_db):
    worker = _empty_worker(ai_registration_db)
    classifiers = {"text_classification": lambda task_id: {}}
    metadata = {"metadata_extraction": lambda task_id: {}}

    assert register_task_processors(worker, classifiers) == ["text_classification"]
    assert register_task_processors(worker, metadata) == ["metadata_extraction"]

    assert worker.get_processor("text_classification") is classifiers["text_classification"]
    assert worker.get_processor("metadata_extraction") is metadata["metadata_extraction"]


def test_register_task_processors_skips_identical_existing(ai_registration_db):
    worker = _empty_worker(ai_registration_db)
    processor = lambda task_id: {}
    worker.register_processor(TEXT_CLASSIFICATION_TASK_TYPE, processor)

    assert register_task_processors(
        worker, {TEXT_CLASSIFICATION_TASK_TYPE: processor}
    ) == [TEXT_CLASSIFICATION_TASK_TYPE]
    assert worker.get_processor(TEXT_CLASSIFICATION_TASK_TYPE) is processor


def test_register_task_processors_is_atomic_on_conflict(ai_registration_db):
    worker = _empty_worker(ai_registration_db)
    existing = lambda task_id: {}
    worker.register_processor("text_classification", existing)

    with pytest.raises(ValueError, match="different processor is already registered"):
        register_task_processors(
            worker,
            {
                "metadata_extraction": lambda task_id: {},
                "text_classification": lambda task_id: {},
            },
        )

    # Nothing from the rejected mapping was applied.
    assert worker.get_processor("metadata_extraction") is None
    assert worker.get_processor("text_classification") is existing


def test_register_task_processors_rejects_invalid_inputs(ai_registration_db):
    worker = _empty_worker(ai_registration_db)

    with pytest.raises(TypeError, match="worker must be an AiWorkerService"):
        register_task_processors(object(), {})
    with pytest.raises(TypeError, match="must be a mapping"):
        register_task_processors(worker, [("text_classification", lambda t: {})])
    with pytest.raises(ValueError, match="task_type must be a non-empty string"):
        register_task_processors(worker, {"   ": lambda t: {}})
    with pytest.raises(TypeError, match="must be callable"):
        register_task_processors(worker, {"text_classification": "nope"})
    assert worker.get_registered_task_types() == []


# ---------------------------------------------------------------------------
# Worker construction
# ---------------------------------------------------------------------------


def test_build_ai_worker_registers_available_processors(tmp_path, ai_registration_db):
    services = _services(tmp_path)

    worker = build_ai_worker(lambda: ai_registration_db(), services)

    assert isinstance(worker, AiWorkerService)
    assert worker.get_registered_task_types() == [TEXT_CLASSIFICATION_TASK_TYPE]
    assert isinstance(
        worker.get_processor(TEXT_CLASSIFICATION_TASK_TYPE),
        TextClassificationProcessor,
    )


def test_build_ai_worker_without_available_models(tmp_path, ai_registration_db):
    services = _services(tmp_path, executable=False)

    worker = build_ai_worker(lambda: ai_registration_db(), services)

    assert isinstance(worker, AiWorkerService)
    assert worker.get_registered_task_types() == []


# ---------------------------------------------------------------------------
# Execution, ownership and task-state behavior
# ---------------------------------------------------------------------------


def test_processor_executes_through_the_worker(
    tmp_path, ai_registration_db, local_storage_adapter
):
    task_id, record_id = _seed_task(ai_registration_db)
    worker = build_ai_worker(lambda: ai_registration_db(), _services(tmp_path))

    assert worker.process_one_task("worker-1", TEXT_CLASSIFICATION_TASK_TYPE) is True

    task = _get_task(ai_registration_db, task_id)
    assert task.status == "AWAITING_APPROVAL"
    assert task.proposal_json["task_id"] == task_id
    assert task.proposal_json["file"]["id"] == record_id
    assert task.proposal_json["classification"]["top_label"] == "document"
    # Ownership is released; the proposal is stored, never applied.
    assert task.claim_token is None
    assert task.worker_id is None
    assert task.lease_expires_at is None
    assert task.decision is None
    assert local_storage_adapter.calls == [("download", "provider-file-1")]


def test_unknown_task_type_is_never_claimed(ai_registration_db):
    task_id, _ = _seed_task(ai_registration_db)
    worker = _empty_worker(ai_registration_db)

    assert worker.process_one_task("worker-1", "metadata_extraction") is False

    task = _get_task(ai_registration_db, task_id)
    assert task.status == "PENDING"
    assert task.claim_token is None
    assert task.worker_id is None
    assert task.lease_expires_at is None


def test_removed_processor_does_not_leave_task_processing(ai_registration_db):
    task_id, _ = _seed_task(ai_registration_db)
    worker = _empty_worker(ai_registration_db)
    worker.register_processor(TEXT_CLASSIFICATION_TASK_TYPE, lambda task_id: {})
    worker.unregister_processor(TEXT_CLASSIFICATION_TASK_TYPE)

    assert worker.process_one_task("worker-1", TEXT_CLASSIFICATION_TASK_TYPE) is False

    assert _get_task(ai_registration_db, task_id).status == "PENDING"


def test_processor_failure_marks_task_failed_and_releases_ownership(
    tmp_path, ai_registration_db, local_storage_adapter
):
    task_id, _ = _seed_task(ai_registration_db)
    runtime = FakeClassificationRuntime(error=RuntimeError("runtime exploded"))
    worker = build_ai_worker(
        lambda: ai_registration_db(), _services(tmp_path, runtime=runtime)
    )

    assert worker.process_one_task("worker-1", TEXT_CLASSIFICATION_TASK_TYPE) is True

    task = _get_task(ai_registration_db, task_id)
    assert task.status == "FAILED"
    assert "runtime exploded" in task.error_message
    assert task.proposal_json is None
    assert task.claim_token is None
    assert task.worker_id is None
    assert task.lease_expires_at is None
    assert task.completed_at is not None


def test_task_keeps_claim_token_and_lease_while_processing(ai_registration_db):
    """A registered processor observes the claimed, leased PROCESSING row."""
    task_id, _ = _seed_task(ai_registration_db)
    worker = _empty_worker(ai_registration_db)
    observed = {}

    def processor(received_task_id):
        db = ai_registration_db()
        try:
            task = db.get(AiTask, received_task_id)
            observed["status"] = task.status
            observed["worker_id"] = task.worker_id
            observed["claim_token"] = task.claim_token
            observed["lease_expires_at"] = task.lease_expires_at
        finally:
            db.close()
        return {"category": "document"}

    worker.register_processor(TEXT_CLASSIFICATION_TASK_TYPE, processor)

    assert worker.process_one_task("worker-1", TEXT_CLASSIFICATION_TASK_TYPE) is True

    assert observed["status"] == "PROCESSING"
    assert observed["worker_id"] == "worker-1"
    assert observed["claim_token"] is not None
    assert observed["lease_expires_at"] > datetime.now(timezone.utc)


def test_heartbeat_still_renews_with_a_registered_processor(ai_registration_db):
    task_id, _ = _seed_task(ai_registration_db)
    worker = _empty_worker(ai_registration_db)
    worker._lease_heartbeat_interval = 0.01

    original_renew = worker.renew_task_lease
    renewed = threading.Event()

    def tracking_renew(task_id, worker_id, claim_token):
        result = original_renew(task_id, worker_id, claim_token)
        renewed.set()
        return result

    worker.renew_task_lease = tracking_renew

    def processor(received_task_id):
        assert renewed.wait(timeout=5), "heartbeat did not renew the lease"
        return {"category": "document"}

    worker.register_processor(TEXT_CLASSIFICATION_TASK_TYPE, processor)

    assert worker.process_one_task("worker-1", TEXT_CLASSIFICATION_TASK_TYPE) is True
    assert _get_task(ai_registration_db, task_id).status == "AWAITING_APPROVAL"


def test_recovered_task_registered_type_is_reprocessed(
    tmp_path, ai_registration_db, local_storage_adapter
):
    task_id, _ = _seed_task(ai_registration_db)
    worker = build_ai_worker(lambda: ai_registration_db(), _services(tmp_path))

    claimed = worker.claim_one_pending_task_by_type(
        "worker-1", TEXT_CLASSIFICATION_TASK_TYPE
    )
    assert claimed is not None and claimed.id == task_id

    db = ai_registration_db()
    try:
        db.execute(
            update(AiTask)
            .where(AiTask.id == task_id)
            .values(
                lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
            )
        )
        db.commit()
    finally:
        db.close()

    assert worker.recover_expired_tasks() == 1
    recovered = _get_task(ai_registration_db, task_id)
    assert recovered.status == "PENDING"
    assert recovered.retries == 1

    assert worker.process_one_task("worker-2", TEXT_CLASSIFICATION_TASK_TYPE) is True
    finished = _get_task(ai_registration_db, task_id)
    assert finished.status == "AWAITING_APPROVAL"
    assert finished.proposal_json["task_id"] == task_id
