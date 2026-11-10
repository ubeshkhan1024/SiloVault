"""Unified file metadata model for SiloVault.

This model represents file metadata consistently across all supported
storage providers (local, Google Drive, OneDrive, Dropbox, Box, MEGA).
It is the central table for the unified file index.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    DateTime,
    Index,
    Integer,
    String,
    TypeDecorator,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class UTCDateTime(TypeDecorator):
    """SQLAlchemy type decorator for UTC-aware timestamps.

    Ensures that outgoing datetime objects are stored as UTC and that incoming
    datetime objects retrieved from SQLite (which returns naive datetimes)
    are restored with explicit ``timezone.utc`` tzinfo. Naive datetime inputs
    are treated as UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: Optional[datetime], dialect: Any
    ) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(
        self, value: Optional[datetime], dialect: Any
    ) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class UnifiedFileRecord(Base):
    """Provider-neutral unified file metadata record.

    Maps native provider payloads into a normalized representation so that
    UI, search, classification, and organization modules operate identically
    regardless of the underlying storage provider.
    """

    __tablename__ = "unified_file_records"

    # Primary key: internal UUID for cross-provider identity (file_<UUID> is 41 chars)
    id: Mapped[str] = mapped_column(
        String(41),
        primary_key=True,
        default=lambda: f"file_{uuid.uuid4()}",
    )

    # Provider identification
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider_account_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    provider_file_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # File identity
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    logical_path: Mapped[str] = mapped_column(String(1024), nullable=False)

    # File properties
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    # Content hash for universal deduplication (SHA-256 hex string)
    sha256: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )

    # Classification metadata
    category: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tags: Mapped[List[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )

    # Sync state machine
    sync_state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="PENDING",
        index=True,
    )

    # Timestamps (UTC-aware)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    last_indexed_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime, nullable=True
    )

    # Provider-specific metadata (native IDs, web links, native hashes, etc.)
    provider_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )

    # Constraints
    __table_args__ = (
        # Prevent duplicate provider file IDs within the same provider account.
        UniqueConstraint(
            "provider",
            "provider_account_id",
            "provider_file_id",
            name="uq_provider_account_file",
        ),
        # Partial unique index for local files (provider='LOCAL' and provider_account_id IS NULL)
        Index(
            "uq_local_provider_file",
            "provider",
            "provider_file_id",
            unique=True,
            sqlite_where=text("provider = 'LOCAL' AND provider_account_id IS NULL"),
        ),
        # Composite indexes for common query patterns
        Index("ix_provider_account", "provider", "provider_account_id"),
        Index("ix_provider_checksum", "provider", "sha256"),
        Index("ix_name_path", "name", "logical_path"),
    )

    def __repr__(self) -> str:
        return (
            f"<UnifiedFileRecord("
            f"id={self.id!r}, "
            f"provider={self.provider!r}, "
            f"name={self.name!r}, "
            f"sync_state={self.sync_state!r}"
            f")>"
        )
