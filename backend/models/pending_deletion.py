"""Pending deletion model for SiloVault.

Tracks storage resources (local files, cloud files) that are awaiting permanent
physical removal after their metadata record has been deleted. This durable
queue ensures that cleanup happens even if the application crashes or the
storage provider is temporarily unavailable.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base
from backend.models.unified_file import UTCDateTime


class PendingDeletion(Base):
    """
    Durable record of a storage resource awaiting permanent cleanup.

    Future status values (handled by later stages):
        • PENDING
        • PROCESSING            – legacy generic flag
        • CLEANUP_IN_PROGRESS
        • RESTORE_IN_PROGRESS
        • FAILED
        • RESTORE_RECOVERY_REQUIRED

    Successful cleanup does **not** persist a SUCCESS status; the row is simply deleted.
    """

    __tablename__ = "pending_deletions"

    # Primary key: internal UUID ("del_<uuid4>", 41 chars)
    id: Mapped[str] = mapped_column(
        String(41),
        primary_key=True,
        default=lambda: f"del_{uuid.uuid4()}",
    )

    # Resource identity — mirrors UnifiedFileRecord exactly
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider_account_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    provider_file_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # Physical handle returned by adapter.quarantine()
    quarantine_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # Human-readable snapshot for logs/admin UI (file record is already gone)
    file_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)

    # Lifecycle: PENDING -> PROCESSING -> (row deleted) | FAILED (retryable)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING", index=True
    )

    # Retry bookkeeping
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(
        String(1024), nullable=True
    )

    # Lease / ownership
    claim_token: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True
    )
    processing_started_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime, nullable=True
    )
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime, nullable=True
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    last_attempted_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime, nullable=True
    )
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime, nullable=True
    )

    # Operation type — CLEANUP or RESTORE
    operation_type: Mapped[str] = mapped_column(
        String(16), nullable=False
    )

    # Snapshot columns – added for Stage A
    original_file_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    original_logical_path: Mapped[Optional[str]] = mapped_column(
        String(1024), nullable=True
    )
    original_mime_type: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    original_size_bytes: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    original_sha256: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    original_category: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    original_tags: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True
    )
    # Generation counter – added for Stage A
    operation_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text('0')
    )

    __table_args__ = (
        # Prevent duplicate cleanup entries for the same cloud resource.
        UniqueConstraint(
            "provider",
            "provider_account_id",
            "provider_file_id",
            name="uq_pending_deletion_resource",
        ),
        # Partial unique index for local files (provider='LOCAL' and provider_account_id IS NULL)
        Index(
            "uq_pending_deletion_local",
            "provider",
            "provider_file_id",
            unique=True,
            sqlite_where=text("provider = 'LOCAL' AND provider_account_id IS NULL"),
        ),
        # Composite indexes for recovery-worker scans
        Index("ix_pending_deletions_status_retry", "status", "next_retry_at"),
        Index("ix_pending_deletions_lease", "lease_expires_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<PendingDeletion("
            f"id={self.id!r}, "
            f"provider={self.provider!r}, "
            f"status={self.status!r}, "
            f"retry_count={self.retry_count}"
            f")>"
        )
