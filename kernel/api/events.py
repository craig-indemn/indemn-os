"""Events stream — Server-Sent Events backed by MongoDB Change Stream. [G-47]

Provides filtered NDJSON event streams for harnesses and consumers.
The events stream watches the message_queue collection and delivers
matching events as newline-delimited JSON.
"""

from __future__ import annotations

from bson import ObjectId
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from kernel.auth.middleware import get_current_actor
from kernel.db import get_database

events_router = APIRouter(prefix="/api/_stream", tags=["events"])


@events_router.get("/events")
async def stream_events(
    actor: str = Query(None),
    interaction: str = Query(None),
    entity_type: str = Query(None),
    current_actor=Depends(get_current_actor),
):
    """Server-Sent Events backed by MongoDB Change Stream. [G-47]"""
    import orjson

    async def event_generator():
        db = get_database()
        org_id = current_actor.org_id

        # Build Change Stream pipeline [G-47]
        match_conditions: dict = {"fullDocument.org_id": org_id}
        if actor:
            match_conditions["$or"] = [
                {"fullDocument.target_actor_id": ObjectId(actor)},
                {"fullDocument.target_actor_id": None},
            ]
        if entity_type:
            match_conditions["fullDocument.entity_type"] = entity_type

        pipeline = [{"$match": match_conditions}]

        async with db["message_queue"].watch(
            pipeline, full_document="updateLookup"
        ) as stream:
            async for change in stream:
                doc = change.get("fullDocument")
                if not doc:
                    continue

                # If interaction filter, check relation
                if interaction:
                    if not await _is_related_to_interaction(doc, interaction):
                        continue

                yield orjson.dumps({
                    "id": str(doc.get("_id")),
                    "entity_type": doc.get("entity_type"),
                    "entity_id": str(doc.get("entity_id", "")),
                    "event_type": doc.get("event_type"),
                    "target_role": doc.get("target_role"),
                    "correlation_id": doc.get("correlation_id"),
                    "event_metadata": doc.get("event_metadata", {}),
                }).decode() + "\n"

    return StreamingResponse(
        event_generator(),
        media_type="application/x-ndjson",
    )


async def _is_related_to_interaction(doc: dict, interaction_id: str) -> bool:
    """Check if a message document is related to a specific interaction."""
    # Check direct reference in context
    context = doc.get("context", {})
    if isinstance(context, dict) and context.get("interaction_id") == interaction_id:
        return True

    # Check entity_id match
    if str(doc.get("entity_id", "")) == interaction_id:
        return True

    # Check event_metadata
    metadata = doc.get("event_metadata", {})
    if isinstance(metadata, dict) and metadata.get("interaction_id") == interaction_id:
        return True

    return False
