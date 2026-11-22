"""Focused tests for the image description task processor."""

import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.ai.execution import ExecutionFailedError
from backend.ai.interfaces import ImageDescriptionResult
from backend.ai.processors import (
    IMAGE_DESCRIPTION_TASK_TYPE,
    FileRecordNotFoundError,
    ImageDescriptionProcessor,
    ImageTooLargeError,
    MissingFileReferenceError,
    ProcessorError,
    TaskNotFoundError,
    UnsupportedImageTypeError,
    UnsupportedTaskTypeError,
)
from backend.database import Base
from backend.models.ai_task import AiTask
from backend.models.unified_file import UnifiedFileRecord
from backend.storage import registry
from backend.storage.capabilities import CAPABILITY_MAP
from backend.storage.errors import StorageUnavailableError
from backend.storage.interfaces import StorageOperations


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class TrackingSession:
    """Wraps a real session and records whether it was closed."""

    def __init__(self, session):
        self._session = session
        self.closed = False

    def close(self):
        self.closed = True
        self._session.close()

    def __getattr__(self, name):
        return getattr(self._session, name)


class TrackingSessionFactory:
    def __init__(self, session_factory):
        self._session_factory = session_factory
        self.sessions = []

    def __call__(self):
        session = TrackingSession(self._session_factory())
        self.sessions.append(session)
        return session


class FakeStorageAdapter(StorageOperations):
    """Storage adapter that records every operation it receives."""

    def __init__(self, content=b"image bytes", error=None):
        self.content = content
        self.error = error
        self.calls = []

    def download(self, provider_file_id):
        self.calls.append(("download", provider_file_id))
        if self.error is not None:
            raise self.error
        return self.content

    def upload(self, file_data, name, logical_path):  # pragma: no cover - forbidden
        self.calls.append(("upload", name))
        raise AssertionError("processor must not upload")

    def rename(self, provider_file_id, new_name):  # pragma: no cover - forbidden
        self.calls.append(("rename", provider_file_id))
        raise AssertionError("processor must not rename")

    def move(self, source_provider_file_id, target_logical_path):  # pragma: no cover
        self.calls.append(("move", source_provider_file_id))
        raise AssertionError("processor must not move")

    def quarantine(self, provider_file_id):  # pragma: no cover - forbidden
        self.calls.append(("quarantine", provider_file_id))
        raise AssertionError("processor must not quarantine")

    def restore(self, quarantine_id):  # pragma: no cover - forbidden
        self.calls.append(("restore", quarantine_id))
        raise AssertionError("processor must not restore")

    def cleanup(self, quarantine_id):  # pragma: no cover - forbidden
        self.calls.append(("cleanup", quarantine_id))
        raise AssertionError("processor must not cleanup")

    @property
    def download_calls(self):
        return [call for call in self.calls if call[0] == "download"]


class FakeExecutionService:
    def __init__(self, result=None, error=None):
        self.result = (
            ImageDescriptionResult(
                description="A photo of a ginger cat sitting in a sunny window.",
                tags=("cat", "window", "pet", "sunlight"),
                model="fake-vision",
                confidence=0.95,
            )
            if result is None
            else result
        )
        self.error = error
        self.describe_calls = []

    def describe_image(self, image, mime_type=None, **kwargs):
        self.describe_calls.append({"image_len": len(image), "mime_type": mime_type})
        if self.error is not None:
            raise self.error
        return self.result


def _lookup_for(adapter):
    calls = []

    def lookup(provider):
        calls.append(provider)
        return adapter, CAPABILITY_MAP.get(provider)

    lookup.calls = calls
    return lookup


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield TestSession
    finally:
        engine.dispose()


def _seed_task(
    Session,
    *,
    task_type=IMAGE_DESCRIPTION_TASK_TYPE,
    file_id=None,
    create_file=True,
    name="image.jpg",
    mime_type="image/jpeg",
    size_bytes=1024,
    provider="LOCAL",
    provider_file_id="provider-1",
    status="PROCESSING",
):
    db = Session()
    try:
        record = None
        if create_file:
            record = UnifiedFileRecord(
                provider=provider,
                provider_account_id=None,
                provider_file_id=provider_file_id,
                name=name,
                logical_path=f"/{name}",
                mime_type=mime_type,
                size_bytes=size_bytes,
            )
            db.add(record)
            db.flush()
            file_id = record.id

        task = AiTask(task_type=task_type, status=status, file_id=file_id)
        db.add(task)
        db.commit()
        task_id = task.id
        record_id = record.id if record is not None else None
    finally:
        db.close()

    return task_id, record_id


def _processor(Session, execution=None, adapter=None, lookup=None, **kwargs):
    adapter = adapter if adapter is not None else FakeStorageAdapter()
    execution = execution if execution is not None else FakeExecutionService()
    lookup = lookup if lookup is not None else _lookup_for(adapter)
    factory = TrackingSessionFactory(Session)
    processor = ImageDescriptionProcessor(
        factory,
        execution,
        storage_lookup=lookup,
        **kwargs,
    )
    return processor, adapter, execution, factory, lookup


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


def test_successful_image_description_proposal(db):
    task_id, record_id = _seed_task(db, size_bytes=1024)
    processor, adapter, execution, factory, lookup = _processor(db)

    proposal = processor(task_id)

    assert adapter.download_calls == [("download", "provider-1")]
    assert lookup.calls == ["LOCAL"]
    assert execution.describe_calls == [{"image_len": 11, "mime_type": "image/jpeg"}]
    assert proposal == {
        "task_id": task_id,
        "task_type": IMAGE_DESCRIPTION_TASK_TYPE,
        "file": {
            "id": record_id,
            "provider": "LOCAL",
            "provider_file_id": "provider-1",
            "name": "image.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 1024,
        },
        "image_understanding": {
            "description": "A photo of a ginger cat sitting in a sunny window.",
            "tags": ["cat", "window", "pet", "sunlight"],
            "model": "fake-vision",
            "confidence": 0.95,
        },
    }
    assert json.loads(json.dumps(proposal)) == proposal


def test_confidence_is_omitted_when_not_provided(db):
    task_id, record_id = _seed_task(db, size_bytes=1024)
    execution = FakeExecutionService(
        result=ImageDescriptionResult(
            description="A cat.",
            tags=("cat",),
            model="vision-model",
            confidence=None,
        )
    )
    processor, _, _, _, _ = _processor(db, execution=execution)

    proposal = processor(task_id)

    assert "confidence" not in proposal["image_understanding"]
    assert proposal["image_understanding"]["description"] == "A cat."
    assert proposal["image_understanding"]["tags"] == ["cat"]


def test_every_created_session_is_closed(db):
    task_id, _ = _seed_task(db)
    processor, _, _, factory, _ = _processor(db)

    processor(task_id)

    assert factory.sessions
    assert all(session.closed for session in factory.sessions)


# ---------------------------------------------------------------------------
# Task and file validation
# ---------------------------------------------------------------------------


def test_missing_task_is_rejected(db):
    processor, adapter, _, factory, _ = _processor(db)

    with pytest.raises(TaskNotFoundError, match="was not found"):
        processor("task_missing")

    assert adapter.calls == []
    assert factory.sessions == [] or all(session.closed for session in factory.sessions)


def test_unsupported_task_type_is_rejected(db):
    task_id, _ = _seed_task(db, task_type="metadata_extraction")
    processor, adapter, execution, _, _ = _processor(db)

    with pytest.raises(UnsupportedTaskTypeError, match="metadata_extraction"):
        processor(task_id)

    assert adapter.calls == []
    assert execution.describe_calls == []


def test_task_without_file_reference_is_rejected(db):
    task_id, _ = _seed_task(db, create_file=False, file_id=None)
    processor, adapter, _, _, _ = _processor(db)

    with pytest.raises(MissingFileReferenceError, match="no associated file"):
        processor(task_id)

    assert adapter.calls == []


def test_task_with_missing_file_record_is_rejected(db):
    task_id, _ = _seed_task(db, create_file=False, file_id="file_missing000")
    processor, adapter, _, _, _ = _processor(db)

    with pytest.raises(FileRecordNotFoundError, match="was not found"):
        processor(task_id)

    assert adapter.calls == []


@pytest.mark.parametrize("task_id", ["", "   ", None, 42])
def test_invalid_task_id_is_rejected(db, task_id):
    processor, adapter, _, factory, _ = _processor(db)

    with pytest.raises(ValueError, match="task_id must be a non-empty string"):
        processor(task_id)

    assert adapter.calls == []
    assert factory.sessions == []


# ---------------------------------------------------------------------------
# Size and MIME type validation (before download)
# ---------------------------------------------------------------------------


def test_oversized_file_is_rejected_before_download(db):
    task_id, _ = _seed_task(db, size_bytes=21 * 1024 * 1024)  # 21 MB
    processor, adapter, execution, _, _ = _processor(db)

    with pytest.raises(ImageTooLargeError, match="maximum for image understanding"):
        processor(task_id)

    assert adapter.calls == []
    assert execution.describe_calls == []


def test_unsupported_mime_type_is_rejected_before_download(db):
    task_id, _ = _seed_task(db, mime_type="text/plain")
    processor, adapter, execution, _, _ = _processor(db)

    with pytest.raises(UnsupportedImageTypeError, match="unsupported image mime type"):
        processor(task_id)

    assert adapter.download_calls == []
    assert execution.describe_calls == []


# ---------------------------------------------------------------------------
# Storage failures
# ---------------------------------------------------------------------------


def test_unavailable_storage_provider_is_rejected(db):
    task_id, _ = _seed_task(db, provider="UNREGISTERED_PROVIDER")
    processor, _, execution, _, _ = _processor(db, lookup=registry.lookup)

    with pytest.raises(StorageUnavailableError, match="Unregistered provider"):
        processor(task_id)

    assert execution.describe_calls == []


def test_download_failure_propagates_without_inference(db):
    task_id, _ = _seed_task(db)
    adapter = FakeStorageAdapter(error=StorageUnavailableError(detail="provider offline"))
    processor, _, execution, _, _ = _processor(db, adapter=adapter)

    with pytest.raises(StorageUnavailableError, match="provider offline"):
        processor(task_id)

    assert adapter.download_calls == [("download", "provider-1")]
    assert execution.describe_calls == []


def test_storage_registry_lookup_is_used_by_default(db):
    task_id, _ = _seed_task(db, provider="TEST_IMAGE_PROVIDER")
    adapter = FakeStorageAdapter()
    registry.register(
        "TEST_IMAGE_PROVIDER", adapter, CAPABILITY_MAP["LOCAL"]
    )
    try:
        processor = ImageDescriptionProcessor(
            lambda: db(), FakeExecutionService()
        )

        proposal = processor(task_id)
    finally:
        registry.remove("TEST_IMAGE_PROVIDER")

    assert proposal["file"]["provider"] == "TEST_IMAGE_PROVIDER"
    assert adapter.download_calls == [("download", "provider-1")]


# ---------------------------------------------------------------------------
# Execution failures
# ---------------------------------------------------------------------------


def test_execution_failure_propagates(db):
    task_id, _ = _seed_task(db)
    execution = FakeExecutionService(error=ExecutionFailedError("runtime exploded"))
    processor, adapter, _, _, _ = _processor(db, execution=execution)

    with pytest.raises(ExecutionFailedError, match="runtime exploded"):
        processor(task_id)

    assert adapter.download_calls == [("download", "provider-1")]


# ---------------------------------------------------------------------------
# No mutation guarantees
# ---------------------------------------------------------------------------


def test_original_metadata_and_task_approval_state_are_unchanged(db):
    task_id, record_id = _seed_task(db)
    session = db()
    try:
        record = session.get(UnifiedFileRecord, record_id)
        task = session.get(AiTask, task_id)
        record_before = {
            "provider": record.provider,
            "provider_file_id": record.provider_file_id,
            "name": record.name,
            "logical_path": record.logical_path,
            "mime_type": record.mime_type,
            "size_bytes": record.size_bytes,
            "category": record.category,
            "tags": list(record.tags),
            "sync_state": record.sync_state,
            "updated_at": record.updated_at,
        }
        task_before = {
            "status": task.status,
            "file_id": task.file_id,
            "proposal_json": task.proposal_json,
            "decision": task.decision,
            "decided_at": task.decided_at,
        }
    finally:
        session.close()

    processor, adapter, _, _, _ = _processor(db)
    processor(task_id)

    session = db()
    try:
        record_after = session.get(UnifiedFileRecord, record_id)
        task_after = session.get(AiTask, task_id)
        assert {
            "provider": record_after.provider,
            "provider_file_id": record_after.provider_file_id,
            "name": record_after.name,
            "logical_path": record_after.logical_path,
            "mime_type": record_after.mime_type,
            "size_bytes": record_after.size_bytes,
            "category": record_after.category,
            "tags": list(record_after.tags),
            "sync_state": record_after.sync_state,
            "updated_at": record_after.updated_at,
        } == record_before
        assert {
            "status": task_after.status,
            "file_id": task_after.file_id,
            "proposal_json": task_after.proposal_json,
            "decision": task_after.decision,
            "decided_at": task_after.decided_at,
        } == task_before
    finally:
        session.close()

    assert adapter.calls == [("download", "provider-1")]


def test_no_provider_write_or_delete_methods_are_called(db):
    task_id, _ = _seed_task(db)
    processor, adapter, _, _, _ = _processor(db)

    processor(task_id)

    destructive = {
        "upload",
        "rename",
        "move",
        "quarantine",
        "restore",
        "cleanup",
    }
    assert [call[0] for call in adapter.calls] == ["download"]
    assert not destructive.intersection(call[0] for call in adapter.calls)


def test_processed_task_can_be_claimed_and_finalized_by_the_worker(db):
    """The returned proposal is exactly what AiWorkerService would store."""
    from backend.services.ai_worker import AiWorkerService

    task_id, record_id = _seed_task(db, status="PENDING")
    adapter = FakeStorageAdapter()
    lookup = _lookup_for(adapter)
    factory = lambda: db()
    processor = ImageDescriptionProcessor(
        factory, FakeExecutionService(), storage_lookup=lookup
    )
    service = AiWorkerService(factory)
    service.register_processor(IMAGE_DESCRIPTION_TASK_TYPE, processor)

    assert service.process_one_task("worker-1", IMAGE_DESCRIPTION_TASK_TYPE)

    session = db()
    try:
        task = session.get(AiTask, task_id)
        assert task.status == "AWAITING_APPROVAL"
        assert task.decision is None
        assert task.decided_at is None
        assert task.proposal_json["task_id"] == task_id
        assert task.proposal_json["file"]["id"] == record_id
    finally:
        session.close()
