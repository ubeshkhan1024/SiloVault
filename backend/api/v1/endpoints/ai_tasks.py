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

router = APIRouter(prefix="/ai/tasks", tags=["ai"])

# Closed set of task types accepted in Stage 1. Extended by later stages.
ALLOWED_TASK_TYPES = frozenset({"text_classification"})

# Terminal states: a task in one of these can never be cancelled.
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


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
    """Cancel a PENDING task, or request cancellation of a running task.

    Semantics:
    - ``PENDING`` tasks are transitioned to ``CANCELLED`` immediately (via a
      guarded conditional UPDATE so a concurrent worker claim wins the race).
    - ``PROCESSING`` / ``AWAITING_APPROVAL`` tasks receive a cancellation
      *request* (``cancellation_requested_at`` is set); the status is left
      unchanged because only the worker can acknowledge and finalize the
      cancellation. The endpoint never claims the task has stopped.
    - Cancelling an already ``CANCELLED`` task is idempotent: it returns 200
      with the current task and changes nothing.
    - ``SUCCEEDED`` / ``FAILED`` are terminal states: cancelling returns
      409 Conflict.
    - Task history is never deleted; this endpoint only changes lifecycle
      fields.

    Returns:
    - 200 with the updated task.
    - 404 if the task does not exist.
    - 409 if the task is in a terminal non-cancellation state.
    """
    task = db.get(AiTask, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AI task '{task_id}' was not found.",
        )

    now = datetime.now(timezone.utc)

    # Terminal states: cancellation is a no-op that cannot succeed.
    if task.status in ("SUCCEEDED", "FAILED"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot cancel task in terminal state: {task.status}"
            ),
        )

    # Already cancelled: idempotent no-op.
    if task.status == "CANCELLED":
        return AiTaskResponse.model_validate(task)

    if task.status == "PENDING":
        # Guarded transition: only the worker (or this endpoint) may move a
        # PENDING task. If the worker claimed it concurrently, rowcount is 0
        # and we re-evaluate based on the fresh state.
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
            db.refresh(task)
            return AiTaskResponse.model_validate(task)

        # The guarded update did not apply — re-read the current state.
        db.expire_all()
        task = db.get(AiTask, task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"AI task '{task_id}' was not found.",
            )
        if task.status in ("SUCCEEDED", "FAILED"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Cannot cancel task in terminal state: {task.status}"
                ),
            )
        # The task was claimed (PROCESSING) or cancelled concurrently —
        # fall through to the running-task / idempotent handling below.

    if task.status in ("PROCESSING", "AWAITING_APPROVAL"):
        # Request cancellation without touching status or claim fields.
        # Guarded so a repeated request never overwrites the first request
        # timestamp and cannot run after the task has been finalized.
        result = db.execute(
            update(AiTask)
            .where(
                AiTask.id == task_id,
                AiTask.status.in_(("PROCESSING", "AWAITING_APPROVAL")),
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
            # The worker finalized the task between our read and update.
            if task.status in ("SUCCEEDED", "FAILED"):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Cannot cancel task in terminal state: {task.status}"
                    ),
                )
            # Otherwise the task was already cancelled by another request;
            # fall through to the idempotent return below.

    # Idempotent: CANCELLED (already or just now) is a success.
    return AiTaskResponse.model_validate(task)
