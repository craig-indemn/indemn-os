"""Direct invocation API — start associate workflows immediately.

Used for real-time channels and testing. Creates a queue entry
for visibility AND starts the workflow immediately.
"""

from uuid import uuid4

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException

from kernel.auth.middleware import get_current_actor
from kernel.message.schema import Message
from kernel.temporal.client import get_temporal_client
from kernel.temporal.workflows import ProcessMessageWorkflow
from kernel_entities.actor import Actor

invoke_router = APIRouter(prefix="/api/associates", tags=["associates"])


@invoke_router.post("/{associate_id}/invoke")
async def invoke_associate(
    associate_id: str,
    context: dict = {},
    actor=Depends(get_current_actor),
):
    """Direct invocation — queue entry + workflow started immediately."""
    associate = await Actor.get(ObjectId(associate_id))
    if not associate:
        raise HTTPException(404, "Associate not found")
    if associate.type != "associate":
        raise HTTPException(400, "Actor is not an associate")

    # Create message in queue for visibility
    message = Message(
        org_id=associate.org_id,
        entity_type=context.get("entity_type", "_direct"),
        entity_id=ObjectId(context.get("entity_id", str(ObjectId()))),
        event_type="direct_invocation",
        target_role="",
        correlation_id=str(uuid4()),
        status="pending",
        context=context,
        summary={"display": f"Direct: {associate.name}"},
    )
    await message.insert()

    # Start workflow immediately
    client = await get_temporal_client()
    if not client:
        return {"message_id": str(message.id), "status": "queued_no_temporal"}

    await client.start_workflow(
        ProcessMessageWorkflow.run,
        args=[str(message.id), associate_id],
        id=f"direct-{message.id}",
        task_queue="indemn-kernel",
    )

    return {"message_id": str(message.id), "status": "invoked"}
