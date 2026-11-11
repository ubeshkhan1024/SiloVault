"""Activity history and audit-log endpoints.

Real implementation backed by the SQLite ``activity_records`` table via the
:class:`ActivityRecord` model. Activities are written only for operations that
have reached their defined success point (see
:mod:`backend.services.activity`).
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.activity import ActivityRecord
from backend.schemas import ActivityListResponse

router = APIRouter(prefix="/activity", tags=["activity"])

# Maximum page size to keep responses bounded.
_MAX_LIMIT = 100


@router.get("", response_model=ActivityListResponse)
async def list_activity(
    action_type: Optional[str] = Query(
        None,
        description="Filter by action type: UPLOAD, DOWNLOAD, or DELETE.",
    ),
    limit: int = Query(
        50, ge=1, le=_MAX_LIMIT, description="Maximum number of items to return."
    ),
    offset: int = Query(0, ge=0, description="Number of items to skip."),
    db: Session = Depends(get_db),
) -> ActivityListResponse:
    """List activity records with pagination and optional filtering.

    Results are ordered newest-first by ``timestamp``; ties on identical timestamps
    are broken deterministically by SQLite row insertion order via ``func.rowid``
    so that concurrent or near-simultaneous operations do not produce flaky
    ordering.

    Parameters
    ----------
    action_type:
        Optional filter restricting results to a single action type.
    limit:
        Maximum number of items to return (1..100).
    offset:
        Number of items to skip from the start of the ordered result set.

    Returns
    -------
    ActivityListResponse
        Paginated list of activity records including the total count that
        matches the applied filters.
    """
    stmt = select(ActivityRecord)
    count_stmt = select(func.count(ActivityRecord.id))

    if action_type:
        stmt = stmt.where(ActivityRecord.action_type == action_type)
        count_stmt = count_stmt.where(ActivityRecord.action_type == action_type)

    stmt = stmt.order_by(
        ActivityRecord.timestamp.desc(),
        ActivityRecord.id.desc(),
    )
    stmt = stmt.limit(limit).offset(offset)

    items = db.execute(stmt).scalars().all()
    total = db.execute(count_stmt).scalar_one()

    return ActivityListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/rollback/{activity_id}")
async def rollback_activity(activity_id: str):
    """Request rollback of a previous move or rename operation.

    Rollback is intentionally NOT implemented at M1: the activity history is
    read-only audit data, and reversible operations require provider adapters
    that do not yet exist. The route is retained for API stability so the
    frontend can keep calling it once rollback is wired up.
    """
    return {
        "status": "placeholder",
        "message": "Activity rollback is not yet implemented at M1.",
        "activity_id": activity_id,
    }