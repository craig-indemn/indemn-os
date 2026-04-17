"""Trace API — unified debugging across changes, messages, and OTEL.

Per vision § 14: "indemn trace entity {id}" queries all three data stores.
"indemn trace cascade {correlation_id}" shows full execution tree.
"""

from bson import ObjectId
from fastapi import APIRouter, Depends, Query

from kernel.api.serialize import to_dict
from kernel.auth.middleware import get_current_actor
from kernel.changes.collection import ChangeRecord
from kernel.context import current_org_id
from kernel.message.schema import Message, MessageLog

trace_router = APIRouter(prefix="/api/trace", tags=["trace"])


@trace_router.get("/entity/{entity_type}/{entity_id}")
async def trace_entity(
    entity_type: str,
    entity_id: str,
    limit: int = Query(50, le=200),
    actor=Depends(get_current_actor),
):
    """Unified timeline for one entity — changes + messages + message log.

    Returns events from all sources merged by timestamp, newest first.
    """
    org_id = current_org_id.get()
    eid = ObjectId(entity_id)

    # Query changes collection
    changes = await ChangeRecord.find(
        {"org_id": org_id, "entity_type": entity_type, "entity_id": eid}
    ).sort("-timestamp").limit(limit).to_list()

    # Query active messages (queue)
    messages = await Message.find(
        {"org_id": org_id, "entity_type": entity_type, "entity_id": eid}
    ).sort("-created_at").limit(limit).to_list()

    # Query completed messages (log)
    message_logs = await MessageLog.find(
        {"org_id": org_id, "entity_type": entity_type, "entity_id": eid}
    ).sort("-completed_at").limit(limit).to_list()

    # Merge into unified timeline
    timeline = []

    for c in changes:
        timeline.append({
            "source": "changes",
            "timestamp": c.timestamp.isoformat() if c.timestamp else None,
            "type": c.change_type,
            "actor_id": c.actor_id,
            "correlation_id": c.correlation_id,
            "method": c.method,
            "changes": [{"field": fc.field, "old": fc.old_value, "new": fc.new_value} for fc in c.changes],
        })

    for m in messages:
        timeline.append({
            "source": "message_queue",
            "timestamp": m.created_at.isoformat() if m.created_at else None,
            "type": "message",
            "status": m.status,
            "target_role": m.target_role,
            "claimed_by": str(m.claimed_by) if m.claimed_by else None,
            "correlation_id": m.correlation_id,
        })

    for ml in message_logs:
        timeline.append({
            "source": "message_log",
            "timestamp": ml.completed_at.isoformat() if ml.completed_at else None,
            "type": "completed",
            "handler_id": str(ml.handler_id) if hasattr(ml, "handler_id") else None,
            "correlation_id": ml.correlation_id,
            "result_summary": str(ml.result)[:200] if hasattr(ml, "result") else None,
        })

    # Sort by timestamp, newest first
    timeline.sort(key=lambda e: e.get("timestamp") or "", reverse=True)

    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "events": timeline[:limit],
        "sources": {
            "changes": len(changes),
            "messages": len(messages),
            "message_logs": len(message_logs),
        },
    }


@trace_router.get("/cascade/{correlation_id}")
async def trace_cascade(
    correlation_id: str,
    limit: int = Query(100, le=500),
    actor=Depends(get_current_actor),
):
    """Execution tree for a correlation_id — all changes + messages in one cascade.

    Returns events linked by correlation_id, showing the full chain from
    trigger to completion across entities and actors.
    """
    org_id = current_org_id.get()

    # Query changes with this correlation_id
    changes = await ChangeRecord.find(
        {"org_id": org_id, "correlation_id": correlation_id}
    ).sort("timestamp").limit(limit).to_list()

    # Query messages with this correlation_id
    messages = await Message.find(
        {"org_id": org_id, "correlation_id": correlation_id}
    ).sort("created_at").limit(limit).to_list()

    message_logs = await MessageLog.find(
        {"org_id": org_id, "correlation_id": correlation_id}
    ).sort("completed_at").limit(limit).to_list()

    # Build execution tree (chronological)
    timeline = []

    for c in changes:
        change_summary = []
        for fc in c.changes:
            change_summary.append(f"{fc.field}: {fc.old_value} → {fc.new_value}")

        timeline.append({
            "source": "changes",
            "timestamp": c.timestamp.isoformat() if c.timestamp else None,
            "entity_type": c.entity_type,
            "entity_id": str(c.entity_id),
            "type": c.change_type,
            "actor_id": c.actor_id,
            "method": c.method,
            "summary": "; ".join(change_summary) if change_summary else c.change_type,
        })

    for m in messages:
        timeline.append({
            "source": "message_queue",
            "timestamp": m.created_at.isoformat() if m.created_at else None,
            "entity_type": m.entity_type,
            "entity_id": str(m.entity_id),
            "type": f"message:{m.status}",
            "target_role": m.target_role,
            "claimed_by": str(m.claimed_by) if m.claimed_by else None,
        })

    for ml in message_logs:
        timeline.append({
            "source": "message_log",
            "timestamp": ml.completed_at.isoformat() if ml.completed_at else None,
            "entity_type": ml.entity_type,
            "entity_id": str(ml.entity_id),
            "type": "completed",
            "handler_id": str(ml.handler_id) if hasattr(ml, "handler_id") else None,
        })

    timeline.sort(key=lambda e: e.get("timestamp") or "")

    # Identify the cascade shape
    entity_types_involved = list({e.get("entity_type") for e in timeline if e.get("entity_type")})
    actors_involved = list({e.get("actor_id") for e in timeline if e.get("actor_id")})

    return {
        "correlation_id": correlation_id,
        "events": timeline[:limit],
        "summary": {
            "total_events": len(timeline),
            "entity_types": entity_types_involved,
            "actors": actors_involved,
            "sources": {
                "changes": len(changes),
                "messages": len(messages),
                "message_logs": len(message_logs),
            },
        },
    }
