"""Human review API — signal HumanReviewWorkflow with decisions.

The UI/CLI calls this endpoint when a human approves, rejects, or escalates
a pending review message.
"""

from fastapi import APIRouter, Depends, HTTPException

from kernel.auth.middleware import get_current_actor
from kernel.message.schema import Message
from kernel.temporal.client import get_temporal_client
from kernel.temporal.workflows import HumanReviewWorkflow

review_router = APIRouter(tags=["messages"])


@review_router.post("/api/messages/{message_id}/decide")
async def submit_decision(
    message_id: str,
    decision: dict,
    actor=Depends(get_current_actor),
):
    """Human submits a decision on a pending review message.
    Signals the HumanReviewWorkflow."""
    message = await Message.get(message_id)
    if not message:
        raise HTTPException(404, "Message not found")

    client = await get_temporal_client()
    if not client:
        raise HTTPException(503, "Temporal not available")

    handle = client.get_workflow_handle(f"human-review-{message_id}")
    await handle.signal(HumanReviewWorkflow.submit_decision, decision)

    return {"status": "decision_submitted"}
