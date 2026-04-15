"""Optimistic dispatch — fire-and-forget Temporal workflow start.

Called AFTER the MongoDB transaction commits (not inside it).
If this fails, the queue processor sweep catches it.
"""

import logging

from kernel.message.schema import Message

logger = logging.getLogger(__name__)


async def optimistic_dispatch(messages: list[Message]):
    """Fire-and-forget Temporal workflow start for associate-eligible messages.

    Primary dispatch path — the queue processor sweep is the backstop.
    """
    from kernel.temporal.client import get_temporal_client
    from kernel.temporal.workflows import HumanReviewWorkflow, ProcessMessageWorkflow
    from kernel_entities.actor import Actor
    from kernel_entities.role import Role

    try:
        client = await get_temporal_client()
    except Exception:
        return  # Temporal unavailable — sweep will handle it

    if not client:
        return

    for message in messages:
        try:
            role = await Role.find_one({
                "name": message.target_role,
                "org_id": message.org_id,
            })
            if not role:
                continue

            # Check for active associates on this role
            associates = await Actor.find({
                "type": "associate",
                "role_ids": role.id,
                "status": "active",
                "org_id": message.org_id,
            }).to_list(length=1)

            if associates:
                # Associate available — ProcessMessageWorkflow
                await client.start_workflow(
                    ProcessMessageWorkflow.run,
                    args=[str(message.id), str(associates[0].id)],
                    id=f"msg-{message.id}",
                    task_queue="indemn-kernel",
                )
            else:
                # No associates — route to HumanReviewWorkflow
                await client.start_workflow(
                    HumanReviewWorkflow.run,
                    args=[str(message.id)],
                    id=f"human-review-{message.id}",
                    task_queue="indemn-kernel",
                )
        except Exception:
            pass  # Fire and forget — sweep catches it
