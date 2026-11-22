"""AI task management API endpoints (M4.1 Stage 1B.1).

API-only layer over the durable ``AiTask`` model. No worker, no inference,
no Ollama integration, no model downloads, and no approval UI are part of
this stage — tasks are created in ``PENDING`` and stay there until a future
worker stage picks them up.

Conventions followed:
* Database session via ``Depends(get_db)`` (same as all other endpoints).
* 404 for missing records, 422 for invalid input, 409 for state conflicts.
* Internal claim/lease fields (``claim_token``, ``worker_id``,
  ``lease_expires_at``) are never settable by clients and never returned.
* Cancellation uses conditional (guarded) database updates so it cannot
  race with a future worker that claims or finalizes the task concurrently.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.ai_task import AiTask
from backend.models.unified_file import UnifiedFileRecord
from backend.schemas import AiTaskCreateRequest, AiTaskListResponse, AiTaskResponse
from backend.services.approval import ApprovalService, InvalidTaskStateError, TaskNotFoundError

router = APIRouter(prefix="/ai/tasks", tags=["ai"])

# Closed set of task types accepted in Stage 1. Extended by later stages.
ALLOWED_TASK_TYPES = frozenset({"text_classification", "image_description"})

# States whose outcome is already final and must never be overwritten by a
# cancellation. ``CANCELLED`` is deliberately excluded: cancelling an
# already-cancelled task is an idempotent success, not a conflict.
UNCANCELLABLE_STATUSES = frozenset({"SUCCEEDED", "FAILED", "REJECTED"})


@router.post("", response_model=AiTaskResponse, status_code=status.HTTP_201_CREATED)
async def create_ai_task(
    body: AiTaskCreateRequest,
    db: Session = Depends(get_db),
) -> AiTaskResponse:
    """Create a new AI task for an existing file.

    The task starts in ``PENDING`` status. No AI processing is executed by
    this endpoint — execution is the responsibility of the future worker.

    Returns:
    - 201 with the created task.
    - 404 if the referenced file does not exist.
    - 422 if the request body is missing fields or the task type is not
      in the allowed set.
    """
    if body.task_type not in ALLOWED_TASK_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Invalid task_type '{body.task_type}'. "
                f"Allowed values: {sorted(ALLOWED_TASK_TYPES)}."
            ),
        )

    file_record = db.get(UnifiedFileRecord, body.file_id)
    if file_record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File with id '{body.file_id}' was not found.",
        )

    task = AiTask(
        file_id=body.file_id,
        task_type=body.task_type,
        status="PENDING",
    )
    db.add(task)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create the AI task.",
        )
    db.refresh(task)
    return AiTaskResponse.model_validate(task)


@router.post("/{task_id}/approve", response_model=AiTaskResponse)
async def approve_ai_task(
    task_id: str,
    db: Session = Depends(get_db),
) -> AiTaskResponse:
    """Approve an AI task in AWAITING_APPROVAL state.

    Transitions status: AWAITING_APPROVAL → SUCCEEDED
    Records decision="APPROVE" and decided_at.
    Clears ownership fields (claim_token, worker_id, lease_expires_at).
    Sets completed_at.

    Returns:
    - 200 with the updated task.
    - 404 if the task does not exist.
    - 409 if the task exists but is not in AWAITING_APPROVAL state.
    """
    service = ApprovalService(lambda: db)
    try:
        updated_task = service.approve_task(task_id)
    except TaskNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AI task '{task_id}' was not found.",
        )
    except InvalidTaskStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    return AiTaskResponse.model_validate(updated_task)


@router.post("/{task_id}/reject", response_model=AiTaskResponse)
async def reject_ai_task(
    task_id: str,
    db: Session = Depends(get_db),
) -> AiTaskResponse:
    """Reject an AI task in AWAITING_APPROVAL state.

    Transitions status: AWAITING_APPROVAL → REJECTED
    Records decision="REJECT" and decided_at.
    Clears ownership fields (claim_token, worker_id, lease_expires_at).
    Sets completed_at.

    Returns:
    - 200 with the updated task.
    - 404 if the task does not exist.
    - 409 if the task exists but is not in AWAITING_APPROVAL state.
    """
    service = ApprovalService(lambda: db)
    try:
        updated_task = service.reject_task(task_id)
    except TaskNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AI task '{task_id}' was not found.",
        )
    except InvalidTaskStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    return AiTaskResponse.model_validate(updated_task)


@router.get("", response_model=AiTaskListResponse)
async def list_ai_tasks(
    file_id: Optional[str] = Query(None, description="Filter by file ID"),
    status_filter: Optional[str] = Query(
        None, alias="status", description="Filter by task status"
    ),
    limit: int = Query(50, ge=1, le=100, description="Maximum number of items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    db: Session = Depends(get_db),
) -> AiTaskListResponse:
    """List AI tasks with pagination and optional filters.

    Results are ordered by ``created_at DESC`` with a stable secondary sort
    by ``id DESC`` (same ordering convention as the file listing endpoint).
    """
    stmt = select(AiTask)
    count_stmt = select(func.count(AiTask.id))

    if file_id:
        stmt = stmt.where(AiTask.file_id == file_id)
        count_stmt = count_stmt.where(AiTask.file_id == file_id)
    if status_filter:
        stmt = stmt.where(AiTask.status == status_filter)
        count_stmt = count_stmt.where(AiTask.status == status_filter)

    stmt = stmt.order_by(AiTask.created_at.desc(), AiTask.id.desc())
    stmt = stmt.limit(limit).offset(offset)

    items = db.execute(stmt).scalars().all()
    total = db.execute(count_stmt).scalar_one()

    return AiTaskListResponse(
        items=[AiTaskResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{task_id}", response_model=AiTaskResponse)
async def get_ai_task(task_id: str, db: Session = Depends(get_db)) -> AiTaskResponse:
    """Retrieve a single AI task by its internal ID.

    Returns:
    - 200 with the task details and current status.
    - 404 if the task does not exist.
    """
    task = db.get(AiTask, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AI task '{task_id}' was not found.",
        )
    return AiTaskResponse.model_validate(task)


@router.delete("/{task_id}", response_model=AiTaskResponse)
async def cancel_ai_task(task_id: str, db: Session = Depends(get_db)) -> AiTaskResponse:
    """Cancel a task according to its current state.

    Behaviour is a closed set (see the dispatch below): ``PENDING`` and
    ``AWAITING_APPROVAL`` become ``CANCELLED`` immediately, ``PROCESSING``
    only records a cancellation request for the worker to finalize,
    ``CANCELLED`` is an idempotent success, and ``SUCCEEDED``, ``FAILED`` and
    ``REJECTED`` are terminal and rejected with 409. Any unrecognised state
    also yields 409 instead of a misleading success.
    """
    task = db.get(AiTask, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AI task '{task_id}' was not found.",
        )

    now = datetime.now(timezone.utc)

    # Deliberate, closed-set cancellation behaviour per state:
    #   CANCELLED          -> 200, no change (idempotent success).
    #   SUCCEEDED/FAILED/
    #   REJECTED           -> 409; the outcome is final and must not be
    #                         overwritten by a cancellation.
    #   PENDING            -> CANCELLED immediately (guarded update).
    #   AWAITING_APPROVAL  -> CANCELLED immediately, ownership cleared
    #                         (guarded update).
    #   PROCESSING         -> cancellation_requested_at only; the worker
    #                         observes it and finalizes the task.
    # Any other state is rejected with 409 rather than reported as success.
    if task.status == "CANCELLED":
        return AiTaskResponse.model_validate(task)

    if task.status in UNCANCELLABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot cancel task in terminal state: {task.status}",
        )

    if task.status == "PENDING":
        result = db.execute(
            update(AiTask)
            .where(
                AiTask.id == task_id,
                AiTask.status == "PENDING",
            )
            .values(status="CANCELLED", completed_at=now)
        )
        db.commit()

        if result.rowcount == 1:
            db.expire_all()
            task = db.get(AiTask, task_id)
            return AiTaskResponse.model_validate(task)

        db.expire_all()
        task = db.get(AiTask, task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"AI task '{task_id}' was not found.",
            )

        if task.status in UNCANCELLABLE_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot cancel task in terminal state: {task.status}",
            )

        if task.status == "CANCELLED":
            return AiTaskResponse.model_validate(task)

    if task.status == "AWAITING_APPROVAL":
        result = db.execute(
            update(AiTask)
            .where(
                AiTask.id == task_id,
                AiTask.status == "AWAITING_APPROVAL",
            )
            .values(
                status="CANCELLED",
                completed_at=now,
                claim_token=None,
                worker_id=None,
                lease_expires_at=None,
            )
        )
        db.commit()
        db.expire_all()
        task = db.get(AiTask, task_id)

        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"AI task '{task_id}' was not found.",
            )

        if result.rowcount == 1 or task.status == "CANCELLED":
            return AiTaskResponse.model_validate(task)

        if task.status in UNCANCELLABLE_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot cancel task in terminal state: {task.status}",
            )

    if task.status == "PROCESSING":
        result = db.execute(
            update(AiTask)
            .where(
                AiTask.id == task_id,
                AiTask.status == "PROCESSING",
                AiTask.cancellation_requested_at.is_(None),
            )
            .values(cancellation_requested_at=now)
        )
        db.commit()
        db.expire_all()
        task = db.get(AiTask, task_id)

        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"AI task '{task_id}' was not found.",
            )

        if result.rowcount != 1 and task.cancellation_requested_at is None:
            if task.status in UNCANCELLABLE_STATUSES:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot cancel task in terminal state: {task.status}",
                )

    # A concurrent worker may have finalized the cancellation between the
    # guarded update and this re-read; that is still the idempotent success.
    if task.status == "CANCELLED":
        return AiTaskResponse.model_validate(task)

    if task.status == "PROCESSING":
        return AiTaskResponse.model_validate(task)

    # Fail closed: no cancellation behaviour is defined for this state, so
    # never report a success that changed nothing.
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"Cannot cancel task in unknown state: {task.status}",
    )
