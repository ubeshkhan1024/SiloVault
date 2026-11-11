"""Activity recording service.

Single entry point for writing successful-operation audit records.

The service exists so that every operation (upload, download, delete) records
its activity through one consistent helper, guaranteeing a uniform schema and
making it trivial to keep failed operations unlogged: callers simply never
call :func:`record_activity` unless the operation has reached its defined
success point.
"""

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from backend.models.activity import ActivityRecord

# Canonical action-type constants. Centralised here so endpoints and tests
# share a single source of truth and cannot drift apart.
ACTION_UPLOAD = "UPLOAD"
ACTION_DOWNLOAD = "DOWNLOAD"
ACTION_DELETE = "DELETE"

# Canonical status.
STATUS_SUCCESS = "SUCCESS"


def record_activity(
    db: Session,
    action_type: str,
    file_name: str,
    provider: str,
    file_id: Optional[str] = None,
    status: str = STATUS_SUCCESS,
    details: Optional[Dict[str, Any]] = None,
) -> ActivityRecord:
    """Persist a successful activity record and return the refreshed instance.

    Parameters
    ----------
    db:
        Active SQLAlchemy session. The record is committed immediately so the
        activity survives even if the surrounding request later fails.
    action_type:
        One of the ``ACTION_*`` constants (UPLOAD, DOWNLOAD, DELETE).
    file_name:
        Snapshot of the original user-facing filename. For DELETE operations
        the file record is already gone, so this is the only identifier we
        keep.
    provider:
        Storage provider the operation targeted.
    file_id:
        Internal unified file ID. ``None`` for DELETE (record removed).
    status:
        Operation status; defaults to ``SUCCESS``.
    details:
        Optional free-form JSON context.

    Returns
    -------
    ActivityRecord
        The committed, refreshed record.

    Raises
    ------
    This function deliberately does not swallow database errors. Callers are
    expected to handle commit failures; a failed commit means no activity was
    written, which is the correct behaviour.
    """
    record = ActivityRecord(
        action_type=action_type,
        file_id=file_id,
        file_name=file_name,
        provider=provider,
        status=status,
        details=details,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record