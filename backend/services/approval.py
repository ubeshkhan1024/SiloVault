"""Approval service for AI task human decisions.

Handles atomic transition of AWAITING_APPROVAL tasks to SUCCEEDED or REJECTED
with decision recording and ownership cleanup.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from backend.models.ai_task import AiTask


class TaskNotFoundError(ValueError):
    """Raised when an AI task ID does not exist."""


class InvalidTaskStateError(ValueError):
    """Raised when an AI task exists but is not in AWAITING_APPROVAL state."""


class ApprovalService:
    """Service for human approval/rejection of AI task proposals."""

    def __init__(self, db_factory) -> None:
        self._db_factory = db_factory

    def approve_task(self, task_id: str, user_id: Optional[str] = None) -> AiTask:
        """
        Approve an AWAITING_APPROVAL task atomically.

        Transitions status: AWAITING_APPROVAL → SUCCEEDED
        Records decision="APPROVE" and decided_at.
        Clears ownership fields (claim_token, worker_id, lease_expires_at).
        Sets completed_at.

        Args:
            task_id: The internal UUID of the AiTask.
            user_id: Optional user identifier for audit purposes (not stored).

        Returns:
            The updated AiTask instance.

        Raises:
            ValueError: If task_id is invalid.
            TaskNotFoundError: If the task ID does not exist.
            InvalidTaskStateError: If the task exists but is not in AWAITING_APPROVAL state.
        """
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id must be a non-empty string")

        now = datetime.now(timezone.utc)
        db: Session = self._db_factory()
        try:
            # Atomic conditional UPDATE: only transition when status is AWAITING_APPROVAL
            update_stmt = (
                update(AiTask)
                .where(
                    AiTask.id == task_id,
                    AiTask.status == "AWAITING_APPROVAL",
                )
                .values(
                    status="SUCCEEDED",
                    decision="APPROVE",
                    decided_at=now,
                    completed_at=now,
                    claim_token=None,
                    worker_id=None,
                    lease_expires_at=None,
                )
                .returning(AiTask)
            )

            result = db.execute(update_stmt)
            updated_task = result.scalar_one_or_none()
            result.close()

            if updated_task is None:
                # Task not found or not in AWAITING_APPROVAL state
                db.rollback()
                # Check if task exists to distinguish 404 vs 409
                existing = db.get(AiTask, task_id)
                if existing is None:
                    raise TaskNotFoundError(f"AI task '{task_id}' was not found.")
                else:
                    raise InvalidTaskStateError(
                        f"Task is not in AWAITING_APPROVAL state (current: {existing.status})"
                    )

            db.commit()
            # Refresh so all attributes are loaded and the detached instance
            # remains readable after the session closes.
            db.refresh(updated_task)
            return updated_task

        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def reject_task(self, task_id: str, user_id: Optional[str] = None) -> AiTask:
        """
        Reject an AWAITING_APPROVAL task atomically.

        Transitions status: AWAITING_APPROVAL → REJECTED
        Records decision="REJECT" and decided_at.
        Clears ownership fields (claim_token, worker_id, lease_expires_at).
        Sets completed_at.

        Args:
            task_id: The internal UUID of the AiTask.
            user_id: Optional user identifier for audit purposes (not stored).

        Returns:
            The updated AiTask instance.

        Raises:
            ValueError: If task_id is invalid.
            TaskNotFoundError: If the task ID does not exist.
            InvalidTaskStateError: If the task exists but is not in AWAITING_APPROVAL state.
        """
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id must be a non-empty string")

        now = datetime.now(timezone.utc)
        db: Session = self._db_factory()
        try:
            # Atomic conditional UPDATE: only transition when status is AWAITING_APPROVAL
            update_stmt = (
                update(AiTask)
                .where(
                    AiTask.id == task_id,
                    AiTask.status == "AWAITING_APPROVAL",
                )
                .values(
                    status="REJECTED",
                    decision="REJECT",
                    decided_at=now,
                    completed_at=now,
                    claim_token=None,
                    worker_id=None,
                    lease_expires_at=None,
                )
                .returning(AiTask)
            )

            result = db.execute(update_stmt)
            updated_task = result.scalar_one_or_none()
            result.close()

            if updated_task is None:
                # Task not found or not in AWAITING_APPROVAL state
                db.rollback()
                # Check if task exists to distinguish 404 vs 409
                existing = db.get(AiTask, task_id)
                if existing is None:
                    raise TaskNotFoundError(f"AI task '{task_id}' was not found.")
                else:
                    raise InvalidTaskStateError(
                        f"Task is not in AWAITING_APPROVAL state (current: {existing.status})"
                    )

            db.commit()
            # Refresh so all attributes are loaded and the detached instance
            # remains readable after the session closes.
            db.refresh(updated_task)
            return updated_task

        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


# Export the service from the services package
__all__ = ["ApprovalService"]