"""Interaction endpoints — handoff, transfer, and observation. [G-49, G-51]

Transfer: move an Interaction between actors/roles.
Observe: start observing an Interaction without handling it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

from kernel.auth.middleware import get_current_actor
from kernel.db import ENTITY_REGISTRY

interaction_router = APIRouter(tags=["interaction"])


@interaction_router.post("/api/interactions/{interaction_id}/transfer")
async def transfer_interaction(
    interaction_id: str,
    to_actor: str = Query(None),
    to_role: str = Query(None),
    actor=Depends(get_current_actor),
):
    """Transfer an Interaction between actors/roles. [G-49]

    Closes old handler's Attention, updates Interaction handler,
    and re-targets pending messages.
    """
    from kernel_entities.attention import Attention

    interaction_cls = ENTITY_REGISTRY.get("Interaction")
    if not interaction_cls:
        raise HTTPException(404, "Interaction entity type not defined")

    interaction = await interaction_cls.get_scoped(interaction_id)
    if not interaction:
        raise HTTPException(404, "Interaction not found")

    old_handler = getattr(interaction, "handling_actor_id", None)

    # 1. Close old actor's Attention [G-49]
    if old_handler:
        old_attentions = await Attention.find({
            "actor_id": ObjectId(str(old_handler)),
            "target_entity.id": ObjectId(interaction_id),
            "status": "active",
        }).to_list()
        for att in old_attentions:
            att.transition_to("closed")
            await att.save_tracked(
                actor_id=str(actor.id), method="handoff_close"
            )

    # 2. Update Interaction handler
    if to_actor:
        interaction.handling_actor_id = ObjectId(to_actor)
        interaction.handling_role_id = None
    elif to_role:
        interaction.handling_role_id = to_role
        interaction.handling_actor_id = None
    else:
        raise HTTPException(400, "Provide to_actor or to_role")

    await interaction.save_tracked(
        actor_id=str(actor.id),
        method="transfer",
        method_metadata={
            "from_actor": str(old_handler) if old_handler else None,
            "to_actor": to_actor,
            "to_role": to_role,
        },
    )

    # 3. Re-target pending messages [G-49]
    if old_handler and to_actor:
        from kernel.message.schema import Message

        await Message.get_motor_collection().update_many(
            {
                "target_actor_id": ObjectId(str(old_handler)),
                "status": "pending",
                "context.interaction_id": interaction_id,
            },
            {"$set": {"target_actor_id": ObjectId(to_actor)}},
        )

    return {"status": "transferred", "to_actor": to_actor, "to_role": to_role}


@interaction_router.post("/api/interactions/{interaction_id}/observe")
async def observe_interaction(
    interaction_id: str,
    actor=Depends(get_current_actor),
):
    """Start observing an Interaction. First-class state — see without handling."""
    from kernel_entities.attention import Attention

    attention = Attention(
        org_id=actor.org_id,
        actor_id=actor.id,
        target_entity={
            "type": "Interaction",
            "id": ObjectId(interaction_id),
        },
        purpose="observing",
        opened_at=datetime.now(timezone.utc),
        last_heartbeat=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    await attention.save_tracked(actor_id=str(actor.id), method="observe")
    return {"attention_id": str(attention.id), "status": "observing"}
