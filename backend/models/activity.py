"""Activity / audit-log model for SiloVault.

Records successful file operations (upload, download, delete) so that the
user can review a persistent operational history. Failed operations are never
recorded: this model is only ever written after an operation has reached its
defined success point.

Design notes
------------
* ``file_id`` is **nullable**. A DELETE activity outlives the file record it
  describes: the row is removed from ``unified_file_records`` before this
  activity is written, so we deliberately keep a ``file_name`` snapshot
  instead of a dangling foreign key.
* ``timestamp`` is UTC-aware (via the shared :class:`UTCDateTime` decorator)
  and indexed because newest-first ordering is the primary access pattern.
* ``action_type`` and ``provider`` are indexed because they are the primary
  filter dimensions of ``GET /api/v1/activity``.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import JSON, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base
from backend.models.unified_file import UTCDateTime


class ActivityRecord(Base):
    """Persistent record of a successful file operation.

    ``status`` defaults to ``SUCCESS``; the model exists specifically for
    successful operations, so a non-success value is not expected in normal
    use but is kept as an explicit column for schema clarity.
    """

    __tablename__ = "activity_records"

    # Primary key: internal UUID ("act_<uuid4>", 40 chars).
    id: Mapped[str] = mapped_column(
        String(41),
        primary_key=True,
        default=lambda: f"act_{uuid.uuid4()}",
    )

    # Operation kind: UPLOAD | DOWNLOAD | DELETE.
    action_type: Mapped[str] = mapped_column(
        String(16), nullable=False
    )

    # Nullable: NULL after a DELETE because the file record no longer exists.
    file_id: Mapped[Optional[str]] = mapped_column(
        String(41), nullable=True
    )

    # Snapshot of the original user-facing filename. Preserved even after the
    # ``unified_file_records`` row is removed, so deleted files remain
    # identifiable in the history.
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Storage provider the operation targeted (e.g. "LOCAL").
    provider: Mapped[str] = mapped_column(String(32), nullable=False)

    # Operation status (normally "SUCCESS").
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="SUCCESS"
    )

    # UTC, timezone-aware timestamp of the successful operation.
    timestamp: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    # Optional free-form context (e.g. upload size / MIME type).
    details: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )

    __table_args__ = (
        # Composite index supporting the dominant "ORDER BY timestamp DESC"
        # access pattern used by the activity API.
        Index("ix_activity_records_timestamp", "timestamp"),
        Index("ix_activity_records_action_type", "action_type"),
        Index("ix_activity_records_provider", "provider"),
    )

    def __repr__(self) -> str:
        return (
            f"<ActivityRecord("
            f"id={self.id!r}, "
            f"action_type={self.action_type!r}, "
            f"file_name={self.file_name!r}, "
            f"provider={self.provider!r}, "
            f"timestamp={self.timestamp!r}"
            f")>"
        )