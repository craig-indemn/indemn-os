"""Queue management API — stats, dead-letter listing, retry.

Provides controlled access to the message queue without exposing
full CRUD on the Message collection.
"""


from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

from kernel.api.serialize import to_dict
from kernel.auth.middleware import get_current_actor
from kernel.message.mongodb_bus import MongoDBMessageBus
from kernel.message.schema import Message

queue_router = APIRouter(tags=["queue"])


@queue_router.get("/api/_meta/queue-stats")
async def queue_stats(actor=Depends(get_current_actor)):
    """Aggregate queue statistics by role and status."""
    pipeline = [
        {"$group": {
            "_id": {"target_role": "$target_role", "status": "$status"},
            "count": {"$sum": 1},
        }},
        {"$sort": {"_id.target_role": 1, "_id.status": 1}},
    ]
    coll = Message.get_motor_collection()
    results = await coll.aggregate(pipeline).to_list(length=100)
    stats = []
    for r in results:
        stats.append({
            "role": r["_id"]["target_role"],
            "status": r["_id"]["status"],
            "count": r["count"],
        })
    return stats


@queue_router.get("/api/message_queues")
async def list_messages(
    status: str = Query(None),
    role: str = Query(None),
    limit: int = Query(20, le=100),
    actor=Depends(get_current_actor),
):
    """List messages in the queue with filters."""
    filter_doc = {}
    if status:
        filter_doc["status"] = status
    if role:
        filter_doc["target_role"] = role
    messages = await Message.find(filter_doc).sort(
        [("priority", -1), ("created_at", 1)]
    ).limit(limit).to_list()
    return [to_dict(m) for m in messages]


@queue_router.post("/api/message_queues/{message_id}/retry")
async def retry_message(
    message_id: str,
    actor=Depends(get_current_actor),
):
    """Retry a dead-lettered or failed message by resetting to pending."""
    message = await Message.get(ObjectId(message_id))
    if not message:
        raise HTTPException(404, "Message not found")
    if message.status not in ("dead_letter", "failed"):
        raise HTTPException(400, f"Cannot retry message in status '{message.status}'")

    await Message.get_motor_collection().update_one(
        {"_id": message.id},
        {
            "$set": {
                "status": "pending",
                "claimed_by": None,
                "visibility_timeout": None,
                "last_error": None,
                "attempt_count": 0,
            }
        },
    )
    return {"status": "retried", "message_id": message_id}


@queue_router.post("/api/messages/claim")
async def claim_message(
    role: str,
    actor=Depends(get_current_actor),
):
    """Claim the next available message for a role. Used by human actors."""
    bus = MongoDBMessageBus()
    message = await bus.claim(role, actor.org_id, actor.id)
    if not message:
        return {"status": "no_messages"}
    return {"status": "claimed", "message": to_dict(message)}


# --- Phase 4 UI routes (aliased for frontend convenience) ---


@queue_router.get("/api/queue/messages")
async def list_queue_messages(
    status: str = Query(None),
    role: str = Query(None),
    limit: int = Query(20, le=100),
    actor=Depends(get_current_actor),
):
    """List queue messages — UI-friendly alias for /api/message_queues."""
    return await list_messages(status=status, role=role, limit=limit, actor=actor)


@queue_router.post("/api/queue/messages/{message_id}/claim")
async def claim_message_by_id(
    message_id: str,
    actor=Depends(get_current_actor),
):
    """Claim a specific message by ID. Used by Queue UI."""
    message = await Message.get(ObjectId(message_id))
    if not message:
        raise HTTPException(404, "Message not found")
    if message.status != "pending":
        raise HTTPException(400, f"Message is {message.status}, not pending")

    await Message.get_motor_collection().update_one(
        {"_id": message.id, "status": "pending"},
        {"$set": {
            "status": "processing",
            "claimed_by": str(actor.id),
        }},
    )
    return {"status": "claimed", "message_id": message_id}


# --- Message lifecycle (standard queue verbs per Q1 session decision) ---


@queue_router.post("/api/message_queues/{message_id}/complete")
async def complete_message(
    message_id: str,
    data: dict = {},
    actor=Depends(get_current_actor),
):
    """Mark a message as completed. Standard queue verb used by any claimer
    (humans via UI, harnesses via CLI). Idempotent — no-op if already terminal."""
    message = await Message.get(ObjectId(message_id))
    if not message:
        raise HTTPException(404, "Message not found")
    if message.status in ("completed", "dead_letter"):
        return {"status": message.status, "message_id": message_id, "idempotent": True}

    bus = MongoDBMessageBus()
    await bus.complete(ObjectId(message_id), data.get("result", {}))
    return {"status": "completed", "message_id": message_id}


@queue_router.post("/api/message_queues/{message_id}/fail")
async def fail_message(
    message_id: str,
    data: dict = {},
    actor=Depends(get_current_actor),
):
    """Mark a message as failed. Standard queue verb used by any claimer.
    Idempotent — no-op if already terminal."""
    message = await Message.get(ObjectId(message_id))
    if not message:
        raise HTTPException(404, "Message not found")
    if message.status in ("completed", "dead_letter"):
        return {"status": message.status, "message_id": message_id, "idempotent": True}

    bus = MongoDBMessageBus()
    await bus.fail(ObjectId(message_id), data.get("reason", "unknown"))
    return {"status": "failed", "message_id": message_id}
