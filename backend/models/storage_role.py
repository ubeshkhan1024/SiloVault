"""Storage role model for SiloVault multi-account architecture.

Defines the ``StorageRole`` ORM model representing a named role that can be
assigned to one or more storage accounts.  Roles enable grouping accounts by
function or permission level (e.g. ``"admin"``, ``"reader"``, ``"writer"``).

Design notes
------------
* ``id`` uses ``str(uuid.uuid4())`` (36-char UUID with hyphens) for
  consistency with ``StorageAccount``.
* ``name`` is unique across the entire role table — no two roles may share
  the same name, regardless of which accounts they are assigned to.
* ``description`` is optional free-form text.
* ``created_at`` records the UTC timestamp when the role was defined.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class StorageRole(Base):
    """Represents a named role that can be assigned to storage accounts.

    Roles support many-to-many relationships with accounts via the
    ``StorageAccountRole`` association table.

    Attributes
    ----------
    id : str
        Stable UUID identity (36 chars).
    name : str
        Unique role name used for lookups and assignment.
    description : Optional[str]
        Human-readable description of the role's purpose.
    created_at : datetime
        UTC-aware timestamp of role creation.
    """

    __tablename__ = "storage_roles"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )

    name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
    )

    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<StorageRole(id={self.id!r}, name={self.name!r})>"