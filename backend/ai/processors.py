"""AI task processors that connect the worker to the existing AI services.

The processor in this module accepts a ``task_id`` exactly like a callable
registered with :class:`~backend.services.ai_worker.AiWorkerService`.  It only
reads task and file metadata, downloads the file bytes through the storage
registry, extracts text, and asks the execution bridge for a classification.

Nothing here mutates the original file, provider data, file metadata, or the
task's approval state: the returned dictionary is a *proposal* that the worker
stores for later human review.  Failures propagate so the worker can record the
task as failed using its existing handling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from backend.ai.execution import (
    AiExecutionService,
    IMAGE_DESCRIPTION_CAPABILITY,
    MAX_IMAGE_DOWNLOAD_BYTES,
    TEXT_CLASSIFICATION_CAPABILITY,
)
from backend.ai.interfaces import ImageDescriptionResult
from backend.ai.text_extraction import (
    MAX_TEXT_EXTRACTION_BYTES,
    ExtractedText,
    FileTooLargeError,
    TextExtractionService,
)
from backend.models.ai_task import AiTask
from backend.models.unified_file import UnifiedFileRecord
from backend.storage.registry import lookup as registry_lookup

#: Task type handled by :class:`TextClassificationProcessor`.  It matches the
#: capability name used by the execution bridge.
TEXT_CLASSIFICATION_TASK_TYPE = TEXT_CLASSIFICATION_CAPABILITY

#: Task type handled by :class:`ImageDescriptionProcessor`. It matches the
#: capability name used by the execution bridge.
IMAGE_DESCRIPTION_TASK_TYPE = IMAGE_DESCRIPTION_CAPABILITY

#: Image MIME types accepted for image understanding. The execution bridge
#: performs the authoritative content-based validation; this set is the
#: metadata-level gate applied before any download.
SUPPORTED_IMAGE_MIME_TYPES = frozenset({
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/bmp",
    "image/tiff",
})


# ---------------------------------------------------------------------------
# Shared exception hierarchy
# ---------------------------------------------------------------------------


class ProcessorError(RuntimeError):
    """Base class for AI task processor failures."""


class TaskNotFoundError(ProcessorError):
    """No AI task exists for the supplied task identifier."""


class UnsupportedTaskTypeError(ProcessorError):
    """The task exists but its type is not handled by this processor."""


class MissingFileReferenceError(ProcessorError):
    """The task has no associated file record reference."""


class FileRecordNotFoundError(ProcessorError):
    """The referenced unified file record does not exist."""


class ImageTooLargeError(ProcessorError):
    """The referenced image exceeds the maximum accepted byte size."""


class UnsupportedImageTypeError(ProcessorError):
    """The referenced file is not a supported image type."""


# ---------------------------------------------------------------------------
# Shared data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TaskSnapshot:
    """Detached copy of the metadata needed after the database session closes."""

    task_id: str
    file_id: str
    provider: str
    provider_file_id: str
    name: str
    mime_type: str
    size_bytes: int


# ---------------------------------------------------------------------------
# Text classification processor
# ---------------------------------------------------------------------------


class TextClassificationProcessor:
    """Processes ``text_classification`` tasks into reviewable proposals.

    Dependencies are injected so the processor can be exercised with fake
    sessions, storage adapters, and execution services:

    * ``session_factory`` creates SQLAlchemy sessions (the same factory the
      worker uses).
    * ``execution_service`` provides ``classify_text(text) -> dict[str, float]``.
    * ``extraction_service`` defaults to :class:`TextExtractionService`.
    * ``storage_lookup`` defaults to the storage registry's ``lookup``.
    """

    def __init__(
        self,
        session_factory: Callable[[], Any],
        execution_service: AiExecutionService,
        *,
        extraction_service: Optional[TextExtractionService] = None,
        storage_lookup: Optional[Callable[[str], tuple[Any, Any]]] = None,
        max_bytes: Optional[int] = None,
    ) -> None:
        if not callable(session_factory):
            raise TypeError("session_factory must be callable")
        if not callable(getattr(execution_service, "classify_text", None)):
            raise TypeError("execution_service must provide classify_text()")
        if extraction_service is None:
            extraction_service = TextExtractionService()
        if not callable(getattr(extraction_service, "extract", None)):
            raise TypeError("extraction_service must provide extract()")
        if storage_lookup is None:
            storage_lookup = registry_lookup
        if not callable(storage_lookup):
            raise TypeError("storage_lookup must be callable")
        if max_bytes is None:
            max_bytes = getattr(
                extraction_service, "max_bytes", MAX_TEXT_EXTRACTION_BYTES
            )
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
            raise TypeError("max_bytes must be an integer")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")

        self._session_factory = session_factory
        self._execution = execution_service
        self._extraction = extraction_service
        self._storage_lookup = storage_lookup
        self._max_bytes = max_bytes

    @property
    def max_bytes(self) -> int:
        """Return the maximum file size accepted before downloading."""
        return self._max_bytes

    def __call__(self, task_id: str) -> dict[str, Any]:
        """Process *task_id*, matching the worker's processor contract."""
        return self.process(task_id)

    def process(self, task_id: str) -> dict[str, Any]:
        """Build a classification proposal for the given AI task.

        Raises :class:`ProcessorError` subclasses for task and file reference
        problems, :class:`~backend.ai.text_extraction.FileTooLargeError` before
        downloading an oversized file, and the storage, extraction, or
        execution exceptions unchanged when those steps fail.
        """
        task_id = _validate_task_id(task_id)
        snapshot = self._load_snapshot(task_id)

        if snapshot.size_bytes > self._max_bytes:
            raise _oversized(snapshot, self._max_bytes)

        adapter = self._resolve_adapter(snapshot.provider)
        content = adapter.download(snapshot.provider_file_id)
        extracted = self._extraction.extract(content, snapshot.name, snapshot.mime_type)
        scores = self._execution.classify_text(extracted.text)

        return _build_text_proposal(snapshot, extracted, scores)

    def _load_snapshot(self, task_id: str) -> _TaskSnapshot:
        db = self._session_factory()
        try:
            task = db.get(AiTask, task_id)
            if task is None:
                raise TaskNotFoundError(f"AI task '{task_id}' was not found")
            if task.task_type != TEXT_CLASSIFICATION_TASK_TYPE:
                raise UnsupportedTaskTypeError(
                    f"AI task '{task_id}' has unsupported task type "
                    f"'{task.task_type}'"
                )
            if not task.file_id:
                raise MissingFileReferenceError(
                    f"AI task '{task_id}' has no associated file record"
                )

            record = db.get(UnifiedFileRecord, task.file_id)
            if record is None:
                raise FileRecordNotFoundError(
                    f"File record '{task.file_id}' referenced by task "
                    f"'{task_id}' was not found"
                )

            return _TaskSnapshot(
                task_id=task.id,
                file_id=record.id,
                provider=record.provider,
                provider_file_id=record.provider_file_id,
                name=record.name,
                mime_type=record.mime_type,
                size_bytes=record.size_bytes,
            )
        finally:
            db.close()

    def _resolve_adapter(self, provider: str) -> Any:
        adapter, _capabilities = self._storage_lookup(provider)
        return adapter


# ---------------------------------------------------------------------------
# Image description processor
# ---------------------------------------------------------------------------


class ImageDescriptionProcessor:
    """Processes ``image_description`` tasks into reviewable proposals.

    Dependencies are injected so the processor can be exercised with fake
    sessions, storage adapters, and execution services:

    * ``session_factory`` creates SQLAlchemy sessions (the same factory the
      worker uses).
    * ``execution_service`` provides ``describe_image(image, mime_type)``.
    * ``storage_lookup`` defaults to the storage registry's ``lookup``.
    """

    def __init__(
        self,
        session_factory: Callable[[], Any],
        execution_service: AiExecutionService,
        *,
        storage_lookup: Optional[Callable[[str], tuple[Any, Any]]] = None,
        max_bytes: Optional[int] = None,
    ) -> None:
        if not callable(session_factory):
            raise TypeError("session_factory must be callable")
        if not callable(getattr(execution_service, "describe_image", None)):
            raise TypeError("execution_service must provide describe_image()")
        if storage_lookup is None:
            storage_lookup = registry_lookup
        if not callable(storage_lookup):
            raise TypeError("storage_lookup must be callable")
        if max_bytes is None:
            max_bytes = MAX_IMAGE_DOWNLOAD_BYTES
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
            raise TypeError("max_bytes must be an integer")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")

        self._session_factory = session_factory
        self._execution = execution_service
        self._storage_lookup = storage_lookup
        self._max_bytes = max_bytes

    @property
    def max_bytes(self) -> int:
        """Return the maximum image size accepted before downloading."""
        return self._max_bytes

    def __call__(self, task_id: str) -> dict[str, Any]:
        """Process *task_id*, matching the worker's processor contract."""
        return self.process(task_id)

    def process(self, task_id: str) -> dict[str, Any]:
        """Build an image understanding proposal for the given AI task.

        Raises :class:`ProcessorError` subclasses for task, file reference,
        size, and type problems, and the storage or execution exceptions
        unchanged when those steps fail.
        """
        task_id = _validate_task_id(task_id)
        snapshot = self._load_snapshot(task_id)

        if snapshot.size_bytes > self._max_bytes:
            raise _image_oversized(snapshot, self._max_bytes)

        _require_supported_image_type(snapshot.mime_type)

        adapter = self._resolve_adapter(snapshot.provider)
        content = adapter.download(snapshot.provider_file_id)
        try:
            result = self._execution.describe_image(content, snapshot.mime_type)
        finally:
            # Ensure image bytes are not retained after inference completes.
            content = None

        return _build_image_proposal(snapshot, result)

    def _load_snapshot(self, task_id: str) -> _TaskSnapshot:
        db = self._session_factory()
        try:
            task = db.get(AiTask, task_id)
            if task is None:
                raise TaskNotFoundError(f"AI task '{task_id}' was not found")
            if task.task_type != IMAGE_DESCRIPTION_TASK_TYPE:
                raise UnsupportedTaskTypeError(
                    f"AI task '{task_id}' has unsupported task type "
                    f"'{task.task_type}'"
                )
            if not task.file_id:
                raise MissingFileReferenceError(
                    f"AI task '{task_id}' has no associated file record"
                )

            record = db.get(UnifiedFileRecord, task.file_id)
            if record is None:
                raise FileRecordNotFoundError(
                    f"File record '{task.file_id}' referenced by task "
                    f"'{task_id}' was not found"
                )

            return _TaskSnapshot(
                task_id=task.id,
                file_id=record.id,
                provider=record.provider,
                provider_file_id=record.provider_file_id,
                name=record.name,
                mime_type=record.mime_type,
                size_bytes=record.size_bytes,
            )
        finally:
            db.close()

    def _resolve_adapter(self, provider: str) -> Any:
        adapter, _capabilities = self._storage_lookup(provider)
        return adapter


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _validate_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id must be a non-empty string")
    return task_id.strip()


# ---------------------------------------------------------------------------
# Text classification helpers
# ---------------------------------------------------------------------------


def _oversized(snapshot: _TaskSnapshot, max_bytes: int) -> FileTooLargeError:
    return FileTooLargeError(
        f"file '{snapshot.file_id}' is {snapshot.size_bytes} bytes; the "
        f"maximum for text extraction is {max_bytes} bytes"
    )


def _build_text_proposal(
    snapshot: _TaskSnapshot,
    extracted: ExtractedText,
    scores: dict[str, float],
) -> dict[str, Any]:
    top_label: Optional[str] = None
    top_score: Optional[float] = None
    if scores:
        top_label, top_score = max(
            scores.items(), key=lambda item: (item[1], item[0])
        )

    return {
        "task_id": snapshot.task_id,
        "task_type": TEXT_CLASSIFICATION_TASK_TYPE,
        "file": {
            "id": snapshot.file_id,
            "provider": snapshot.provider,
            "provider_file_id": snapshot.provider_file_id,
            "name": snapshot.name,
            "mime_type": snapshot.mime_type,
            "size_bytes": snapshot.size_bytes,
        },
        "extraction": {
            "extension": extracted.extension,
            "byte_size": extracted.byte_size,
            "text_length": len(extracted.text),
        },
        "classification": {
            "scores": dict(scores),
            "top_label": top_label,
            "top_score": top_score,
        },
    }


# ---------------------------------------------------------------------------
# Image description helpers
# ---------------------------------------------------------------------------


def _image_oversized(snapshot: _TaskSnapshot, max_bytes: int) -> ImageTooLargeError:
    return ImageTooLargeError(
        f"file '{snapshot.file_id}' is {snapshot.size_bytes} bytes; the "
        f"maximum for image understanding is {max_bytes} bytes"
    )


def _require_supported_image_type(mime_type: str) -> None:
    if not isinstance(mime_type, str) or not mime_type.strip():
        raise UnsupportedImageTypeError("file has no usable mime type")
    normalised = mime_type.split(";", 1)[0].strip().casefold()
    if normalised not in SUPPORTED_IMAGE_MIME_TYPES:
        raise UnsupportedImageTypeError(
            f"unsupported image mime type '{mime_type}'"
        )


def _build_image_proposal(
    snapshot: _TaskSnapshot,
    result: ImageDescriptionResult,
) -> dict[str, Any]:
    understanding: dict[str, Any] = {
        "description": result.description,
        "tags": list(result.tags),
        "model": result.model,
    }
    # Confidence is never fabricated: only included when the runtime supplied it.
    if result.confidence is not None:
        understanding["confidence"] = result.confidence

    return {
        "task_id": snapshot.task_id,
        "task_type": IMAGE_DESCRIPTION_TASK_TYPE,
        "file": {
            "id": snapshot.file_id,
            "provider": snapshot.provider,
            "provider_file_id": snapshot.provider_file_id,
            "name": snapshot.name,
            "mime_type": snapshot.mime_type,
            "size_bytes": snapshot.size_bytes,
        },
        "image_understanding": understanding,
    }
