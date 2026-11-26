"""Storage account-role association model for SiloVault multi-account architecture.

Defines the ``StorageAccountRole`` ORM model representing the many-to-many
relationship between accounts and roles.  Each association records when the
role was assigned to the account.

Design notes
------------
* The composite primary key ``(account_id, role_id)`` enforces that any
  given account can be assigned to a given role at most once.
* ``assigned_at`` records the UTC timestamp of the assignment.
* Foreign keys reference ``storage_accounts.id`` and ``storage_roles.id``
  respectively, maintaining referential integrity at the ORM level.
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class StorageAccountRole(Base):
    """Associates a ``StorageAccount`` with a ``StorageRole``.

    This many-to-many association table tracks which roles have been assigned
    to which accounts and when the assignment occurred.

    Attributes
    ----------
    account_id : str
        Foreign key linking to ``StorageAccount.id`` (composite primary key
        component).
    role_id : str
        Foreign key linking to ``StorageRole.id`` (composite primary key
        component).
    assigned_at : datetime
        UTC-aware timestamp of when the role was assigned to the account.
    """

    __tablename__ = "storage_account_roles"

    account_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("storage_accounts.id"),
        primary_key=True,
    )

    role_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("storage_roles.id"),
        primary_key=True,
    )

    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __init__(self, account_id: str, role_id: str) -> None:
        from datetime import datetime, timezone as tz
        self.account_id = account_id
        self.role_id = role_id
        self.assigned_at = datetime.now(tz.utc)

    def __repr__(self) -> str:
        return (
            f"<StorageAccountRole(account_id={self.account_id!r}, "
            f"role_id={self.role_id!r})>"
        )