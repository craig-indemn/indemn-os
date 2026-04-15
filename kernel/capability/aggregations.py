"""Pipeline metrics — aggregation capabilities.

State distribution, queue depth, throughput metrics.
Callable from CLI/API/UI.
"""

from __future__ import annotations

from bson import ObjectId

from kernel.message.schema import Message


async def state_distribution(entity_cls, org_id: ObjectId) -> dict:
    """Count per state machine value for an entity type."""
    pipeline = [
        {"$match": {"org_id": org_id}},
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]
    result = await entity_cls.get_motor_collection().aggregate(pipeline).to_list(length=100)
    return {doc["_id"]: doc["count"] for doc in result if doc["_id"] is not None}


async def queue_depth(org_id: ObjectId) -> dict:
    """Pending message count per role."""
    pipeline = [
        {"$match": {"org_id": org_id, "status": "pending"}},
        {"$group": {"_id": "$target_role", "count": {"$sum": 1}}},
    ]
    result = await Message.get_motor_collection().aggregate(pipeline).to_list(length=100)
    return {doc["_id"]: doc["count"] for doc in result if doc["_id"] is not None}
