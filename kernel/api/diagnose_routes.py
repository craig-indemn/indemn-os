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
from kernel.message.schema import Message, MessageLog

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
    """Recent runs for an actor — unified view across queue, log, and traces.

    Aggregates from three sources:
      1. message_queue — in-flight + failed + dead_letter (not yet completed)
      2. message_log   — completed runs (the bulk of historical activity)
      3. traces        — LLM execution records for reasoning/hybrid associates

    Pre-fix this only queried message_queue, which silently missed completed
    runs (they're moved to message_log on completion). MC/TS/IE/Evaluator
    showed count=0 because their work completes — moving to log — while the
    failed/dead_letter case is rare. Closes CLI gap #10.

    Returns per-run: message_id, entity_type, entity_id, status, duration_ms,
    correlation_id, plus trace fields (trace_id, langsmith_run_id,
    execution_status, total_tokens) when a Trace exists for that run.
    """
    org_id = current_org_id.get()

    from kernel_entities.actor import Actor
    from kernel_entities.role import Role
    from kernel_entities.trace import Trace

    target_actor = await Actor.find_one({"_id": ObjectId(actor_id), "org_id": org_id})
    if not target_actor:
        return {"error": "Actor not found", "actor_id": actor_id}

    role_name = None
    role_ids = getattr(target_actor, "role_ids", [])
    if role_ids:
        role_entity = await Role.find_one({"_id": role_ids[0]})
        if role_entity:
            role_name = role_entity.name

    if not role_name:
        return {"error": "Actor has no role — cannot query messages", "actor_id": actor_id}

    # 1. In-flight / failed / dead_letter messages (still in queue)
    queue_msgs = await Message.find(
        {
            "org_id": org_id,
            "target_role": role_name,
            "status": {"$in": ["failed", "dead_letter", "processing", "pending", "parked"]},
        }
    ).sort("-created_at").limit(limit).to_list()

    # 2. Completed messages — moved to log on bus.complete()
    log_msgs = await MessageLog.find(
        {"org_id": org_id, "target_role": role_name}
    ).sort("-created_at").limit(limit).to_list()

    # 3. Trace entities for this specific actor — LLM run records, regardless
    # of whether the message lifecycle matches up cleanly. Catches Evaluator-
    # style runs and any case where the trace exists but the queue path took
    # an unusual route.
    traces = await Trace.find(
        {"org_id": org_id, "associate_id": str(actor_id)}
    ).sort("-created_at").limit(limit).to_list()

    # Merge by message_id (or trace_id for traces without a matching message).
    by_key: dict = {}

    def _row(message_id_str: str) -> dict:
        if message_id_str not in by_key:
            by_key[message_id_str] = {"message_id": message_id_str}
        return by_key[message_id_str]

    for msg in queue_msgs:
        claimed_at = getattr(msg, "claimed_at", None)
        duration_ms = None
        if claimed_at and msg.created_at:
            duration_ms = (claimed_at - msg.created_at).total_seconds() * 1000
        _row(str(msg.id)).update({
            "source": "message_queue",
            "entity_type": msg.entity_type,
            "entity_id": str(msg.entity_id) if msg.entity_id else None,
            "status": msg.status,
            "attempt_count": getattr(msg, "attempt_count", 0),
            "last_error": getattr(msg, "last_error", None),
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
            "claimed_at": claimed_at.isoformat() if claimed_at else None,
            "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
            "correlation_id": msg.correlation_id,
        })

    for msg in log_msgs:
        completed_at = getattr(msg, "completed_at", None)
        claimed_at = getattr(msg, "claimed_at", None)
        # Prefer the queue-to-completion duration (claimed_at -> completed_at)
        # over created_at -> completed_at, which conflates queue wait time
        # with actual work.
        duration_ms = None
        if completed_at and claimed_at:
            duration_ms = (completed_at - claimed_at).total_seconds() * 1000
        _row(str(msg.id)).update({
            "source": "message_log",
            "entity_type": msg.entity_type,
            "entity_id": str(msg.entity_id) if msg.entity_id else None,
            "status": "completed",
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
            "claimed_at": claimed_at.isoformat() if claimed_at else None,
            "completed_at": completed_at.isoformat() if completed_at else None,
            "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
            "correlation_id": msg.correlation_id,
        })

    # Layer trace info on top of any matching message rows; for orphan traces
    # (no matching message row found), add them as their own rows keyed by
    # message_id from the trace.
    for tr in traces:
        msg_id = getattr(tr, "message_id", None)
        if not msg_id:
            continue
        row = _row(str(msg_id))
        row.update({
            "trace_id": str(tr.id),
            "langsmith_run_id": getattr(tr, "langsmith_run_id", None),
            "execution_status": getattr(tr, "execution_status", None),
            "total_tokens": getattr(tr, "total_tokens", None),
        })
        # If neither queue nor log carried this row, mark its source as trace-only
        if "source" not in row:
            row["source"] = "trace_only"
            row["created_at"] = tr.created_at.isoformat() if getattr(tr, "created_at", None) else None
            row["entity_type"] = getattr(tr, "entity_type", None)
            row["entity_id"] = str(tr.entity_id) if getattr(tr, "entity_id", None) else None
            row["correlation_id"] = getattr(tr, "correlation_id", None)

    runs = sorted(
        by_key.values(),
        key=lambda r: r.get("created_at") or "",
        reverse=True,
    )[:limit]

    return {
        "actor_id": actor_id,
        "actor_name": getattr(target_actor, "name", None),
        "role": role_name,
        "runs": runs,
        "count": len(runs),
        "sources": {
            "message_queue": len(queue_msgs),
            "message_log": len(log_msgs),
            "traces": len(traces),
        },
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

    claimed_at = getattr(msg, "claimed_at", None)
    visibility_timeout = getattr(msg, "visibility_timeout", None)

    result = {
        "message_id": str(msg.id),
        "entity_type": msg.entity_type,
        "entity_id": str(msg.entity_id) if msg.entity_id else None,
        "target_role": msg.target_role,
        "status": msg.status,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
        "claimed_at": claimed_at.isoformat() if claimed_at else None,
        "visibility_timeout": visibility_timeout.isoformat() if visibility_timeout else None,
        "attempt_count": getattr(msg, "attempt_count", 0),
        "max_attempts": getattr(msg, "max_attempts", 3),
        "last_error": getattr(msg, "last_error", None),
        "correlation_id": msg.correlation_id,
        "causation_id": getattr(msg, "causation_id", None),
        "depth": getattr(msg, "depth", None),
        "event_type": getattr(msg, "event_type", None),
    }

    if claimed_at and msg.created_at:
        result["duration_ms"] = round(
            (claimed_at - msg.created_at).total_seconds() * 1000, 1
        )

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
    from kernel_entities.role import Role

    target_actor = await Actor.find_one({"org_id": org_id, "name": actor_name})
    if not target_actor:
        return {"error": "Actor not found", "actor_name": actor_name}

    role_name = None
    role_ids = getattr(target_actor, "role_ids", [])
    if role_ids:
        role_entity = await Role.find_one({"_id": role_ids[0]})
        if role_entity:
            role_name = role_entity.name

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
        claimed_at = getattr(msg, "claimed_at", None)
        if claimed_at and msg.created_at:
            duration_ms = (claimed_at - msg.created_at).total_seconds() * 1000

        ticks.append(
            {
                "message_id": str(msg.id),
                "created_at": msg.created_at.isoformat() if msg.created_at else None,
                "claimed_at": claimed_at.isoformat() if claimed_at else None,
                "status": msg.status,
                "duration_ms": round(duration_ms, 1) if duration_ms else None,
                "attempt_count": getattr(msg, "attempt_count", 0),
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
