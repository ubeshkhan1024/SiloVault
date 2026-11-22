"""Composition of AI task processors with the worker's processor registry.

This module connects the existing pieces only:

* :mod:`backend.ai.processors` builds proposals from task and file metadata,
* :class:`~backend.ai.execution.AiExecutionService` resolves the model/runtime
  through the shared :class:`~backend.ai.interfaces.ModelManager`,
* :class:`~backend.services.ai_worker.AiWorkerService` owns the task registry.

It does not start a worker, run a polling loop, download models, or modify task
records.  Callers decide when a configured worker is used.

A processor is registered only when the model manager reports that the current
profile can execute its capability.  Registering an unusable processor would
claim tasks and immediately mark them ``FAILED``; leaving it unregistered keeps
those tasks ``PENDING`` so they can be processed after a model and runtime are
installed.
"""

from __future__ import annotations

from typing import Callable, Dict, Mapping

from backend.ai.bootstrap import AiModelServices
from backend.ai.execution import IMAGE_DESCRIPTION_CAPABILITY, TEXT_CLASSIFICATION_CAPABILITY, AiExecutionService
from backend.ai.processors import (
    IMAGE_DESCRIPTION_TASK_TYPE,
    TEXT_CLASSIFICATION_TASK_TYPE,
    ImageDescriptionProcessor,
    TextClassificationProcessor,
)

from backend.services.ai_worker import AiWorkerService

#: A callable matching the worker's processor contract.
TaskProcessor = Callable[[str], dict]


def build_task_processors(
    session_factory: Callable[[], object],
    services: AiModelServices,
) -> Dict[str, TaskProcessor]:
    """Build the processors supported by the current AI model services.

    Args:
        session_factory: Creates SQLAlchemy sessions for task and file reads.
            It is passed through to the processors unchanged.
        services: Shared AI model services from
            :func:`backend.ai.bootstrap.build_ai_model_services`.

    Returns:
        task_type -> processor for every capability the current profile can
        execute.  The mapping is empty when no model and compatible runtime
        are available.

    Raises:
        TypeError: If ``session_factory`` is not callable or ``services`` is
            not an :class:`AiModelServices` instance.
    """
    if not callable(session_factory):
        raise TypeError("session_factory must be callable")
    if not isinstance(services, AiModelServices):
        raise TypeError("services must be an AiModelServices instance")

    execution = AiExecutionService(services.manager)
    processors: Dict[str, TaskProcessor] = {}

    # Fail closed: only expose a processor that can actually execute now.
    if services.manager.can_execute_capability(TEXT_CLASSIFICATION_CAPABILITY):
        processors[TEXT_CLASSIFICATION_TASK_TYPE] = TextClassificationProcessor(
            session_factory, execution
        )
    if services.manager.can_execute_capability(IMAGE_DESCRIPTION_CAPABILITY):
        processors[IMAGE_DESCRIPTION_TASK_TYPE] = ImageDescriptionProcessor(
            session_factory, execution
        )

    return processors


def register_task_processors(
    worker: AiWorkerService,
    processors: Mapping[str, TaskProcessor],
) -> list[str]:
    """Register *processors* on *worker* by task type.

    The whole mapping is validated before anything is registered, so a
    conflicting or invalid entry cannot leave the worker partially configured.
    An entry whose callable is already registered for that task type is
    skipped; a different callable for that task type raises.

    Returns:
        The task types registered (or already identical) after this call, in
        mapping order.

    Raises:
        TypeError: If ``worker`` is not an :class:`AiWorkerService`, a task
            type is not a string, a processor is not callable, or
            ``processors`` is not a mapping.
        ValueError: If a task type is blank or a different processor is
            already registered for it.
    """
    if not isinstance(worker, AiWorkerService):
        raise TypeError("worker must be an AiWorkerService instance")
    if not isinstance(processors, Mapping):
        raise TypeError("processors must be a mapping of task type to callable")

    normalized: Dict[str, TaskProcessor] = {}
    for task_type, processor in processors.items():
        if not isinstance(task_type, str) or not task_type.strip():
            raise ValueError("task_type must be a non-empty string")
        task_type = task_type.strip()
        if not callable(processor):
            raise TypeError(f"processor for task_type '{task_type}' must be callable")
        normalized[task_type] = processor

    # Check every entry against the current registry before mutating it.
    for task_type, processor in normalized.items():
        existing = worker.get_processor(task_type)
        if existing is not None and existing is not processor:
            raise ValueError(
                "A different processor is already registered for "
                f"task_type: {task_type}"
            )

    registered: list[str] = []
    for task_type, processor in normalized.items():
        worker.register_processor(task_type, processor)
        registered.append(task_type)
    return registered


def build_ai_worker(
    session_factory: Callable[[], object],
    services: AiModelServices,
) -> AiWorkerService:
    """Build an ``AiWorkerService`` with the available AI processors registered.

    The worker is constructed and configured but not started; task claiming and
    recovery remain explicit caller actions.
    """
    worker = AiWorkerService(session_factory)
    register_task_processors(
        worker, build_task_processors(session_factory, services)
    )
    return worker


__all__ = [
    "TaskProcessor",
    "build_task_processors",
    "register_task_processors",
    "build_ai_worker",
]
