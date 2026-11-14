"""AI task model for SiloVault.

Tracks a unit of AI work against a file (e.g. text classification) as a
durable, restart-safe record. The database row is the source of truth for the
task lifecycle; an in-memory queue is never authoritative.

Design notes
------------
* ``file_id`` is **nullable** and references ``unified_file_records.id`` with
  ``ON DELETE SET NULL``. When a file record is removed (deletion workflow),
  the AI task rows are preserved with ``file_id`` set to NULL so the work
  history and any pending proposals survive. This mirrors the
  :class:`~backend.models.activity.ActivityRecord` convention of keeping a
  reference that outlives the file record.
* Multiple AI tasks may reference the same file — there is deliberately **no**
  unique constraint on ``file_id``.
* ``status`` is a closed state machine:
  ``PENDING -> PROCESSING -> AWAITING_APPROVAL -> SUCCEEDED`` with
  ``FAILED`` and ``CANCELLED`` as terminal branches. ``PENDING`` transitions
  directly to ``PROCESSING`` via an atomic conditional claim (see the future
  worker design); there is no separate QUEUED state.
* Retry bookkeeping (``retries`` / ``max_retries``) is tracked separately from
  ``status`` so a task can be retried without losing its lifecycle position.
* Claim/lease fields (``claim_token`` / ``worker_id`` /
  ``lease_expires_at``) mirror :class:`~backend.models.pending_deletion.
  PendingDeletion` so the AI worker can use the same ownership convention.
* ``proposal_json`` holds AI-generated metadata as a **proposal** only. It is
  never applied to the original file metadata in Stage 1; application of
  approved proposals is a later stage.

This model does not implement the worker, recovery, or any AI runtime.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import ForeignKey, Index, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base
from backend.models.unified_file import UTCDateTime


class AiTask(Base):
    """Durable record of a single AI processing task.

    The row is created when a task is requested and transitions through the
    lifecycle as the (future) worker processes it. The row survives
    application restarts.
    """

    __tablename__ = "ai_tasks"

    # Primary key: internal UUID ("task_<uuid4>", 45 chars).
    id: Mapped[str] = mapped_column(
        String(45),
        primary_key=True,
        default=lambda: f"task_{uuid.uuid4()}",
    )

    # Reference to the file being processed. Nullable + ON DELETE SET NULL so
    # the task survives deletion of the file record (see module docstring).
    file_id: Mapped[Optional[str]] = mapped_column(
        String(41),
        ForeignKey("unified_file_records.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Kind of AI work (e.g. "text_classification").
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)

    # Lifecycle state (closed set). Defaults to PENDING.
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING"
    )

    # Retry bookkeeping, tracked separately from status.
    retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    # Timestamps (UTC-aware).
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime, nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime, nullable=True
    )

    # Cancellation request marker. Set when a cancellation is requested for a
    # task that is already PROCESSING; the worker acknowledges it before
    # finalizing.
    cancellation_requested_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime, nullable=True
    )

    # Claim / lease ownership (mirrors PendingDeletion conventions).
    claim_token: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True
    )
    worker_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime, nullable=True
    )

    # AI-generated metadata proposal (NOT applied to the file in Stage 1).
    proposal_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )

    # Last error, if the task failed.
    error_message: Mapped[Optional[str]] = mapped_column(
        String(1024), nullable=True
    )

    __table_args__ = (
        # Pending-task selection and expired-lease recovery are the dominant
        # access patterns for the worker.
        Index("ix_ai_tasks_status", "status"),
        Index("ix_ai_tasks_lease_expires", "lease_expires_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<AiTask("
            f"id={self.id!r}, "
            f"file_id={self.file_id!r}, "
            f"task_type={self.task_type!r}, "
            f"status={self.status!r}"
            f")>"
        )
