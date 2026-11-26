"""Storage account model for SiloVault multi-account architecture.

Defines the ``StorageAccount`` ORM model representing a connected storage
provider account.  Each account has a stable identity, provider association,
optional credential reference, status tracking, and configuration.

Design notes
------------
* ``id`` uses ``str(uuid.uuid4())`` (36-char UUID with hyphens) to match
  the specification and avoid collisions with the ``uuid4().hex`` pattern
  used elsewhere in the codebase for file IDs.
* ``credentials_ref`` is optional because LOCAL accounts may have no
  authentication state.
* ``credentials_ref`` uniqueness is additionally enforced by a
  **migration-owned** partial unique expression index
  (``uq_storage_accounts_credentials_ref_trimmed``, over
  ``trim(credentials_ref, char(9,10,11,12,13,32))`` where the column is not
  NULL).  It is deliberately not declared on the model: SQLite reflection
  skips expression-based indexes, so a metadata declaration would make
  Alembic autogenerate try to recreate it.  Python's ``str.strip()`` in
  ``AccountManager`` remains authoritative for broader Unicode whitespace.
* ``status`` defaults to ``"pending"`` on initial connection.
* ``configuration`` uses SQLAlchemy JSON column with a Python-side default
  of ``dict`` so that each record gets its own independent mapping.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class StorageAccount(Base):
    """Represents a connected storage provider account.

    Attributes
    ----------
    id : str
        Stable UUID identity (36 chars, "xxxx-xxxx-xxxx-xxxx-xxxx" format).
    provider_id : str
        Identifier matching the provider registry (e.g. ``"GOOGLE_DRIVE"``,
        ``"LOCAL"``).
    display_name : str
        User-defined account name.
    credentials_ref : Optional[str]
        Opaque reference to account-specific credentials.  ``None`` for
        accounts that do not use credential-based auth (e.g. LOCAL).
    status : str
        Current connection status.  Defaults to ``"pending"``.
    configuration : dict
        Provider-specific configuration stored as JSON.
    created_at : datetime
        UTC-aware timestamp of account creation.
    updated_at : datetime
        UTC-aware timestamp of last update; auto-updated on mutation.
    """

    __tablename__ = "storage_accounts"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )

    provider_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )

    display_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    credentials_ref: Mapped[Optional[str]] = mapped_column(
        String(512),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
    )

    configuration: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<StorageAccount(id={self.id!r}, "
            f"provider_id={self.provider_id!r}, "
            f"display_name={self.display_name!r}, "
            f"status={self.status!r})>"
        )