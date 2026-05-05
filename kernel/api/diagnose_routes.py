"""Diagnose API — operational diagnostics for associates and queue messages.

Replaces mongosh + Railway logs + aws CLI for debugging stuck runs.
All endpoints scoped to the authenticated actor's org_id.
"""

from datetime import datetime
from decimal import Decimal

from bson import ObjectId
from fastapi import APIRouter, Depends, Query

from kernel.auth.middleware import get_current_actor
from kernel.context import current_org_id
from kernel.message.schema import Message

diagnose_router = APIRouter(prefix="/api/_diagnose", tags=["diagnose"])


def _safe(v):
    if isinstance(v, ObjectId):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, list):
        return [_safe(i) for i in v]
    if isinstance(v, dict):
        return {k: _safe(val) for k, val in v.items()}
    return v


@diagnose_router.get("/actor/{actor_id}")
async def diagnose_actor(
    actor_id: str,
    limit: int = Query(10, le=100),
    actor=Depends(get_current_actor),
):
    """Recent runs for an actor — messages claimed by this actor's role, with outcomes.

    Returns per-run: message_id, entity_type, entity_id, status, duration_ms,
    attempt_count, last_error, created_at, completed_at.
    """
    org_id = current_org_id.get()

    from kernel_entities.actor import Actor

    target_actor = await Actor.find_one({"_id": ObjectId(actor_id), "org_id": org_id})
    if not target_actor:
        return {"error": "Actor not found", "actor_id": actor_id}

    role_name = target_actor.role if hasattr(target_actor, "role") else None
    if not role_name:
        roles = getattr(target_actor, "roles", [])
        role_name = roles[0] if roles else None

    if not role_name:
        return {"error": "Actor has no role — cannot query messages", "actor_id": actor_id}

    messages = (
        await Message.find(
            {
                "org_id": org_id,
                "target_role": role_name,
                "status": {"$in": ["completed", "failed", "dead_letter"]},
            }
        )
        .sort("-created_at")
        .limit(limit)
        .to_list()
    )

    runs = []
    for msg in messages:
        duration_ms = None
        if msg.completed_at and msg.created_at:
            duration_ms = (msg.completed_at - msg.created_at).total_seconds() * 1000

        runs.append(
            {
                "message_id": str(msg.id),
                "entity_type": msg.entity_type,
                "entity_id": str(msg.entity_id) if msg.entity_id else None,
                "status": msg.status,
                "attempt_count": getattr(msg, "attempt_count", None),
                "last_error": getattr(msg, "last_error", None),
                "created_at": msg.created_at.isoformat() if msg.created_at else None,
                "completed_at": (
                    msg.completed_at.isoformat() if getattr(msg, "completed_at", None) else None
                ),
                "duration_ms": round(duration_ms, 1) if duration_ms else None,
                "correlation_id": msg.correlation_id,
            }
        )

    return {
        "actor_id": actor_id,
        "actor_name": getattr(target_actor, "name", None),
        "role": role_name,
        "runs": runs,
        "count": len(runs),
    }


@diagnose_router.get("/message/{message_id}")
async def diagnose_message(
    message_id: str,
    actor=Depends(get_current_actor),
):
    """Full lifecycle of a queue message — claims, extensions, transitions, errors."""
    org_id = current_org_id.get()

    msg = await Message.find_one({"_id": ObjectId(message_id), "org_id": org_id})
    if not msg:
        return {"error": "Message not found", "message_id": message_id}

    result = {
        "message_id": str(msg.id),
        "entity_type": msg.entity_type,
        "entity_id": str(msg.entity_id) if msg.entity_id else None,
        "target_role": msg.target_role,
        "status": msg.status,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
        "claimed_at": (
            getattr(msg, "claimed_at", None).isoformat()
            if getattr(msg, "claimed_at", None)
            else None
        ),
        "completed_at": (
            getattr(msg, "completed_at", None).isoformat()
            if getattr(msg, "completed_at", None)
            else None
        ),
        "visibility_timeout": (
            getattr(msg, "visibility_timeout", None).isoformat()
            if getattr(msg, "visibility_timeout", None)
            else None
        ),
        "attempt_count": getattr(msg, "attempt_count", None),
        "max_attempts": getattr(msg, "max_attempts", None),
        "last_error": getattr(msg, "last_error", None),
        "correlation_id": msg.correlation_id,
        "causation_id": getattr(msg, "causation_id", None),
        "depth": getattr(msg, "depth", None),
        "event_type": getattr(msg, "event_type", None),
    }

    duration_ms = None
    if getattr(msg, "completed_at", None) and msg.created_at:
        duration_ms = (msg.completed_at - msg.created_at).total_seconds() * 1000
        result["duration_ms"] = round(duration_ms, 1)

    return result


@diagnose_router.get("/cron")
async def diagnose_cron(
    actor_name: str = Query(..., description="Actor name to query cron runs for"),
    limit: int = Query(10, le=100),
    actor=Depends(get_current_actor),
):
    """Last N cron ticks for a scheduled actor — per-tick duration + outcome.

    Queries messages where entity_type='_scheduled' targeting this actor's role.
    """
    org_id = current_org_id.get()

    from kernel_entities.actor import Actor

    target_actor = await Actor.find_one({"org_id": org_id, "name": actor_name})
    if not target_actor:
        return {"error": "Actor not found", "actor_name": actor_name}

    role_name = target_actor.role if hasattr(target_actor, "role") else None
    if not role_name:
        roles = getattr(target_actor, "roles", [])
        role_name = roles[0] if roles else None

    messages = (
        await Message.find(
            {
                "org_id": org_id,
                "target_role": role_name,
                "entity_type": "_scheduled",
            }
        )
        .sort("-created_at")
        .limit(limit)
        .to_list()
    )

    ticks = []
    for msg in messages:
        duration_ms = None
        completed_at = getattr(msg, "completed_at", None)
        if completed_at and msg.created_at:
            duration_ms = (completed_at - msg.created_at).total_seconds() * 1000

        ticks.append(
            {
                "message_id": str(msg.id),
                "created_at": msg.created_at.isoformat() if msg.created_at else None,
                "completed_at": completed_at.isoformat() if completed_at else None,
                "status": msg.status,
                "duration_ms": round(duration_ms, 1) if duration_ms else None,
                "attempt_count": getattr(msg, "attempt_count", None),
                "last_error": getattr(msg, "last_error", None),
            }
        )

    return {
        "actor_name": actor_name,
        "actor_id": str(target_actor.id),
        "role": role_name,
        "ticks": ticks,
        "count": len(ticks),
    }
