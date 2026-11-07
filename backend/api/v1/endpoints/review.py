"""Human review and approval queue endpoints.

PLACEHOLDER — These routes are defined for future implementation.
No organization suggestion generation or approval workflow exists yet.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("/pending")
async def list_pending_approvals():
    """List organization suggestions awaiting human approval.

    TODO: Implement approval queue query against the SQLite database.
    """
    return {
        "status": "placeholder",
        "message": "Approval queue is not yet implemented.",
        "pending_approvals": [],
    }


@router.post("/{approval_id}/action")
async def submit_approval_action(approval_id: str, action_data: dict = None):
    """Submit a human decision (approve, modify, or reject) for a suggestion.

    TODO: Implement approval action processing and file move execution.
    Expected body: {"action": "APPROVE|MODIFY|REJECT", "custom_filename": null, "custom_tags": null}
    """
    return {
        "status": "placeholder",
        "message": "Approval action is not yet implemented.",
        "approval_id": approval_id,
        "received_action": action_data.get("action") if action_data else None,
    }