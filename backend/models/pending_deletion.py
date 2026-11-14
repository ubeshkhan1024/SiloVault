"""Pending deletion model for SiloVault.

Tracks storage resources (local files, cloud files) that are awaiting permanent
physical removal after their metadata record has been deleted. This durable
queue ensures that cleanup happens even if the application crashes or the
storage provider is temporarily unavailable.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base
from backend.models.unified_file import UTCDateTime


class PendingDeletion(Base):
    """Durable record of a storage resource awaiting permanent cleanup.

    Written atomically in the same transaction that removes the
    ``UnifiedFileRecord``. Survives crashes between the DB commit and the
    provider cleanup call; consumed by the recovery service.
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
        String(16), nullable=False, default="PENDING", index=True
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
