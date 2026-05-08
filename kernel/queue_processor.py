"""Queue processor — sweep process.

Runs extensible sweep functions each cycle.
Phase 1: visibility timeout recovery + escalation deadline checks.
Phase 2 adds: Temporal workflow dispatch for associate-eligible messages,
              scheduled associate execution.
Phase 5 adds: Attention TTL cleanup + zombie detection.

Entry point: python -m kernel.queue_processor
"""

import asyncio
import logging
import signal
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from kernel.db import init_database
from kernel.message.schema import Message
from kernel.observability.logging import setup_logging

logger = logging.getLogger(__name__)

# Extensible sweep functions — phases register additional sweeps
_sweep_functions: list[callable] = []


def register_sweep(func: callable):
    """Register a sweep function to run each cycle."""
    _sweep_functions.append(func)


# --- Phase 1 sweep functions ---


async def check_visibility_timeouts():
    """Find processing messages past their visibility timeout.
    Dead-letter ones that have exceeded `max_attempts`; recover the rest
    to pending for re-claiming.

    Bug #50 fix — pre-fix this function unconditionally recovered every
    timed-out message back to pending without consulting `max_attempts`.
    The next claim incremented `attempt_count` (per `mongodb_bus.claim`),
    but nothing ever capped it, so messages stuck in a slow-subprocess /
    short-visibility race could attempt 7+ times indefinitely. The
    explicit `bus.fail` path enforces max_attempts; this implicit recovery
    path was the gap. Mirror the same enforcement here."""
    now = datetime.now(timezone.utc)

    # Step 1: dead-letter messages that have exceeded max_attempts. The
    # `$expr` clause compares two fields on the same document; the
    # message-bus claim path uses `$inc: {attempt_count: 1}` on every
    # successful claim, so by the time visibility expires after attempt N,
    # `attempt_count == N` already.
    dead_lettered = await Message.get_motor_collection().update_many(
        {
            "status": "processing",
            "visibility_timeout": {"$lt": now},
            "$expr": {"$gte": ["$attempt_count", "$max_attempts"]},
        },
        {
            "$set": {
                "status": "dead_letter",
                "last_error": (
                    "Visibility timeout exceeded with attempt_count >= max_attempts "
                    "(Bug #50: visibility-recovery path now caps retries)"
                ),
            },
        },
    )

    # Step 2: recover the rest to pending for re-claim.
    recovered = await Message.get_motor_collection().update_many(
        {
            "status": "processing",
            "visibility_timeout": {"$lt": now},
        },
        {
            "$set": {"status": "pending", "claimed_by": None, "visibility_timeout": None},
        },
    )
    if dead_lettered.modified_count > 0:
        logger.info(
            "Dead-lettered %d messages over max_attempts (Bug #50)",
            dead_lettered.modified_count,
        )
    if recovered.modified_count > 0:
        logger.info("Recovered %d timed-out messages", recovered.modified_count)


async def check_escalation_deadlines():
    """Find pending messages past their due_by deadline."""
    now = datetime.now(timezone.utc)
    overdue = await Message.find(
        {
            "status": "pending",
            "due_by": {"$lt": now, "$ne": None},
        }
    ).to_list()
    for msg in overdue:
        logger.warning("Message %s past deadline (due_by: %s)", msg.id, msg.due_by)


# Register Phase 1 sweeps
register_sweep(check_visibility_timeouts)
register_sweep(check_escalation_deadlines)


# --- Phase 2 sweep functions ---


# Temporal workflow statuses we treat as "terminal" — workflow is gone,
# message stayed at pending = orphan. Anything not in this set (RUNNING,
# CONTINUED_AS_NEW) leaves the message alone. See Bug #38 root cause #2.
_TEMPORAL_TERMINAL_STATUSES = frozenset(
    [
        "COMPLETED",
        "FAILED",
        "CANCELED",
        "TERMINATED",
        "TIMED_OUT",
    ]
)


async def _mark_message_dead_letter(message, reason: str) -> None:
    """Direct motor update — bypasses Pydantic load (status-only change,
    no need for the full entity round-trip; Bug #37-class loads can fail
    on unrelated malformed fields)."""
    coll = message.get_motor_collection()
    await coll.update_one(
        {"_id": message.id},
        {
            "$set": {
                "status": "dead_letter",
                "last_error": reason,
            },
        },
    )


async def _reemit_parked(parked_message) -> "Message":
    """Re-emit a parked message as a fresh message with a new ID.

    The parked message retires to dead_letter (preserving audit trail).
    The fresh message dispatches cleanly with a new workflow ID.
    """
    fresh = Message(
        org_id=parked_message.org_id,
        entity_type=parked_message.entity_type,
        entity_id=parked_message.entity_id,
        event_type=parked_message.event_type,
        target_role=parked_message.target_role,
        target_actor_id=parked_message.target_actor_id,
        correlation_id=parked_message.correlation_id,
        causation_id=f"reemit:{parked_message.id}",
        depth=parked_message.depth,
        priority=parked_message.priority,
        event_metadata=parked_message.event_metadata,
        context=parked_message.context,
        summary=parked_message.summary,
    )
    await fresh.insert()

    coll = parked_message.get_motor_collection()
    await coll.update_one(
        {"_id": parked_message.id},
        {"$set": {"status": "dead_letter", "last_error": f"Re-emitted as {fresh.id}"}},
    )
    logger.info(
        "Re-emitted parked message %s as fresh %s (role=%s entity=%s:%s)",
        parked_message.id,
        fresh.id,
        parked_message.target_role,
        parked_message.entity_type,
        parked_message.entity_id,
    )
    return fresh


async def _mark_message_parked(message, reason: str) -> None:
    """Park message status. Dispatch sweep re-evaluates parked alongside
    pending so the message dispatches once the actor reactivates."""
    coll = message.get_motor_collection()
    await coll.update_one(
        {"_id": message.id},
        {
            "$set": {
                "status": "parked",
                "last_error": reason,
            },
        },
    )


async def _handle_already_started(message, client, workflow_id: str) -> None:
    """The workflow_id collided with one Temporal already knows about
    (Bug #38 root cause #1 + #2). Determine if it's still running or
    in a terminal state, and clean up the orphaned message accordingly.

    RUNNING / CONTINUED_AS_NEW → leave message alone; workflow will
        progress (or its own retry policy will end it terminally
        and the next sweep can clean up).
    Terminal (COMPLETED/FAILED/CANCELED/TERMINATED/TIMED_OUT) → workflow
        is gone but message stayed at pending. Mark dead_letter so
        the sweep stops trying to redispatch it every cycle.
    Describe failure (Temporal flake, permission issue) → log and
        continue; next sweep retries the diagnosis."""
    try:
        handle = client.get_workflow_handle(workflow_id)
        desc = await handle.describe()
        status_name = getattr(desc.status, "name", str(desc.status))
    except Exception as e:
        logger.warning(
            "Could not describe existing workflow %s for message %s: %s",
            workflow_id,
            message.id,
            e,
        )
        return

    if status_name in _TEMPORAL_TERMINAL_STATUSES:
        await _mark_message_dead_letter(
            message,
            f"Orphaned: workflow {workflow_id} ended {status_name} but "
            f"message status stayed pending (Bug #38 cleanup).",
        )
        logger.info(
            "Bug #38 cleanup: message %s → dead_letter (workflow %s was %s)",
            message.id,
            workflow_id,
            status_name,
        )
    else:
        # RUNNING or CONTINUED_AS_NEW — workflow is in flight, leave alone
        logger.debug(
            "Message %s skipped: workflow %s still %s",
            message.id,
            workflow_id,
            status_name,
        )


async def _dispatch_one_message(message, client) -> None:
    """Per-message dispatch logic.

    Decision tree:
        1. Resolve role by message.target_role.
        2. Find active associates for that role.
            a. If active associate exists → start ProcessMessageWorkflow.
            b. If no active but suspended-or-other associate-type actor
               exists → park the message (Bug #38 root cause #3). The
               role IS autonomous; the operator just suspended it. Don't
               route to HumanReviewWorkflow (that creates work no human
               will action).
            c. If no associate-type actor exists at all → human role;
               route to HumanReviewWorkflow per existing fallback.
        3. On WorkflowAlreadyStartedError → clean up orphan (Bug #38
           root cause #1 + #2 via _handle_already_started).

    Extracted from dispatch_associate_workflows so the catch behavior
    is testable in isolation (see tests/unit/test_dispatch_workflow_already_started.py)."""
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from kernel.temporal.workflows import HumanReviewWorkflow, ProcessMessageWorkflow
    from kernel_entities.actor import Actor
    from kernel_entities.role import Role

    role = await Role.find_one(
        {
            "name": message.target_role,
            "org_id": message.org_id,
        }
    )
    if not role:
        return

    # Active associates — happy path
    active = await Actor.find(
        {
            "type": "associate",
            "role_ids": role.id,
            "status": "active",
            "org_id": message.org_id,
        }
    ).to_list()

    if active:
        dispatch_message = message
        if message.status == "parked":
            dispatch_message = await _reemit_parked(message)

        workflow_id = f"msg-{dispatch_message.id}"
        try:
            await client.start_workflow(
                ProcessMessageWorkflow.run,
                args=[str(dispatch_message.id), str(active[0].id)],
                id=workflow_id,
                task_queue="indemn-kernel",
            )
        except WorkflowAlreadyStartedError:
            await _handle_already_started(dispatch_message, client, workflow_id)
        return

    # No active associates — distinguish "suspended autonomous role" from
    # "human role with no associates at all". Bug #38 root cause #3.
    all_associates = await Actor.find(
        {
            "type": "associate",
            "role_ids": role.id,
            "org_id": message.org_id,
        }
    ).to_list()

    if all_associates:
        # Autonomous role, but every actor is suspended/provisioned/etc.
        # Park the message. Next sweep re-evaluates; when operator
        # reactivates an actor, dispatch fires.
        await _mark_message_parked(
            message,
            f"No active associate for role {role.name} "
            f"(found {len(all_associates)} non-active).",
        )
        return

    # Truly human role (no associate actors at all) — keep existing
    # HumanReviewWorkflow fall-through.
    workflow_id = f"human-review-{message.id}"
    try:
        await client.start_workflow(
            HumanReviewWorkflow.run,
            args=[str(message.id)],
            id=workflow_id,
            task_queue="indemn-kernel",
        )
    except WorkflowAlreadyStartedError:
        await _handle_already_started(message, client, workflow_id)


async def dispatch_associate_workflows():
    """Find pending messages for roles with associates.
    Start Temporal workflows for them.

    This is the SWEEP BACKSTOP — optimistic dispatch from the API
    is the primary path. This catches anything that was missed.

    Parked messages are NOT auto-dispatched. Use `indemn queue drain`
    to explicitly re-emit parked messages at a controlled pace.
    This separates "process new work" from "replay historical backlog."
    """
    from kernel.temporal.client import get_temporal_client

    client = await get_temporal_client()
    if not client:
        return  # Temporal not configured

    threshold = datetime.now(timezone.utc) - timedelta(seconds=10)
    messages = (
        await Message.find(
            {
                "status": "pending",
                "created_at": {"$lt": threshold},
            }
        )
        .sort([("created_at", 1)])
        .to_list(length=100)
    )

    if not messages:
        return

    for message in messages:
        try:
            await _dispatch_one_message(message, client)
        except Exception as e:
            logger.error("Error dispatching message %s: %s", message.id, e)


async def check_scheduled_associates():
    """Check for associates with schedule triggers whose cron has fired. [G-76]"""
    from croniter import croniter

    from kernel_entities.actor import Actor
    from kernel_entities.role import Role

    associates = await Actor.find(
        {
            "type": "associate",
            "status": "active",
            "trigger_schedule": {"$exists": True, "$ne": None},
        }
    ).to_list()

    now = datetime.now(timezone.utc)

    for associate in associates:
        try:
            cron = croniter(associate.trigger_schedule, now - timedelta(minutes=1))
            next_fire = cron.get_next(datetime)
            if next_fire <= now:
                # Check if we already created a message for this firing
                existing = await Message.find_one(
                    {
                        "entity_type": "_scheduled",
                        "entity_id": associate.id,
                        "created_at": {"$gte": now - timedelta(minutes=1)},
                    }
                )
                if existing:
                    continue

                # Resolve role name from role_id
                role_name = ""
                if associate.role_ids:
                    role = await Role.get(associate.role_ids[0])
                    role_name = role.name if role else ""

                # Create message in queue — same path as watch-triggered work
                message = Message(
                    org_id=associate.org_id,
                    entity_type="_scheduled",
                    entity_id=associate.id,
                    event_type="schedule_fired",
                    target_role=role_name,
                    correlation_id=str(uuid4()),
                    status="pending",
                    summary={"display": f"Scheduled: {associate.name}"},
                )
                await message.insert()
                logger.info("Created scheduled message for associate %s", associate.name)

        except Exception as e:
            logger.error("Error checking schedule for %s: %s", associate.name, e)


# Register Phase 2 sweeps [G-67]
register_sweep(dispatch_associate_workflows)
register_sweep(check_scheduled_associates)


# --- Phase 5 sweep functions ---


async def cleanup_expired_attentions():
    """Find and expire Attentions past their TTL. [G-44]"""
    from kernel_entities.attention import Attention

    now = datetime.now(timezone.utc)
    expired = await Attention.find(
        {
            "status": "active",
            "expires_at": {"$lt": now},
        }
    ).to_list()

    for attention in expired:
        attention.transition_to("expired")
        await attention.save_tracked(
            actor_id="system:ttl_cleanup",
            method="ttl_expiration",
        )
        logger.info("Expired attention %s (TTL reached)", attention.id)


async def handle_zombie_sessions():
    """Detect and recover from zombie real-time sessions. [G-45]

    When a Runtime crashes, its Attentions expire via TTL.
    This sweep finds recently expired real-time sessions and
    transitions the linked entity to 'abandoned'.
    """
    from kernel.db import ENTITY_REGISTRY
    from kernel_entities.attention import Attention

    now = datetime.now(timezone.utc)
    recently_expired = await Attention.find(
        {
            "status": "expired",
            "purpose": "real_time_session",
            "expires_at": {"$gte": now - timedelta(minutes=5)},
        }
    ).to_list()

    for attention in recently_expired:
        target = attention.target_entity
        entity_type = target.get("type")
        entity_id = target.get("id")
        if not entity_type or not entity_id:
            continue

        entity_cls = ENTITY_REGISTRY.get(entity_type)
        if not entity_cls:
            continue

        entity = await entity_cls.get(entity_id)
        if not entity:
            continue

        # Only transition if entity is still in active state
        current_state = getattr(entity, "status", None) or getattr(entity, "stage", None)
        if current_state == "active":
            try:
                entity.transition_to("abandoned")
                await entity.save_tracked(
                    actor_id="system:zombie_recovery",
                    method="zombie_recovery",
                    method_metadata={
                        "attention_id": str(attention.id),
                        "reason": "Runtime session expired",
                    },
                )
                logger.warning(
                    "Zombie recovery: %s %s transitioned to abandoned",
                    entity_type,
                    entity_id,
                )
            except Exception as exc:
                logger.error("Zombie recovery failed for %s %s: %s", entity_type, entity_id, exc)


# Register Phase 5 sweeps
register_sweep(cleanup_expired_attentions)
register_sweep(handle_zombie_sessions)


# --- Cascade completion detection ---


_CASCADE_IDLE_SECONDS = 120


@register_sweep
async def check_cascade_completion():
    """Detect completed cascades and create cascade-level evaluator messages.

    A cascade is "complete" when all messages sharing a correlation_id have
    reached terminal status (completed/failed/dead_letter) and no new messages
    have been created for that correlation_id within the idle threshold.

    Creates a single evaluator message per completed cascade with
    event_type=cascade_eval, so the evaluator can assess the full cascade.
    """
    from kernel_entities.trace import Trace

    now = datetime.now(timezone.utc)
    idle_cutoff = now - timedelta(seconds=_CASCADE_IDLE_SECONDS)

    pipeline = [
        {"$match": {"correlation_id": {"$ne": None, "$exists": True}}},
        {"$group": {
            "_id": "$correlation_id",
            "total": {"$sum": 1},
            "pending": {"$sum": {"$cond": [{"$eq": ["$status", "pending"]}, 1, 0]}},
            "processing": {"$sum": {"$cond": [{"$eq": ["$status", "processing"]}, 1, 0]}},
            "parked": {"$sum": {"$cond": [{"$eq": ["$status", "parked"]}, 1, 0]}},
            "last_created": {"$max": "$created_at"},
            "roles": {"$addToSet": "$target_role"},
        }},
        {"$match": {
            "pending": 0,
            "processing": 0,
            "parked": 0,
            "last_created": {"$lt": idle_cutoff},
        }},
        {"$limit": 20},
    ]

    completed_cascades = await Message.get_motor_collection().aggregate(pipeline).to_list(length=20)

    for cascade in completed_cascades:
        corr_id = cascade["_id"]
        if not corr_id or corr_id.startswith("reemit:"):
            continue

        existing_eval = await Message.get_motor_collection().find_one({
            "correlation_id": corr_id,
            "event_type": "cascade_eval",
            "target_role": "evaluator",
        })
        if existing_eval:
            continue

        traces = await Trace.get_motor_collection().find(
            {"correlation_id": corr_id}
        ).to_list(length=50)
        if not traces:
            continue

        org_id = traces[0].get("org_id")
        if not org_id:
            continue

        from kernel_entities.role import Role
        from kernel_entities.actor import Actor

        evaluator_role = await Role.get_motor_collection().find_one(
            {"org_id": org_id, "name": "evaluator"}
        )
        if not evaluator_role:
            continue

        active_evaluator = await Actor.get_motor_collection().find_one(
            {"org_id": org_id, "role_ids": evaluator_role["_id"], "status": "active"}
        )
        if not active_evaluator:
            continue

        msg = Message(
            org_id=org_id,
            entity_type="Trace",
            entity_id=traces[0]["_id"],
            event_type="cascade_eval",
            target_role="evaluator",
            correlation_id=corr_id,
            causation_id=corr_id,
            depth=0,
            context={
                "cascade_correlation_id": corr_id,
                "trace_ids": [str(t["_id"]) for t in traces],
                "trace_count": len(traces),
            },
        )
        await msg.insert()
        logger.info("Cascade eval triggered for correlation_id=%s (%d traces)", corr_id, len(traces))


# --- Sweep loop ---


async def run_sweep_cycle():
    """Run all registered sweep functions."""
    for func in _sweep_functions:
        try:
            await func()
        except Exception as e:
            logger.error("Sweep function %s failed: %s", func.__name__, e)


# --- Entry point ---

_running = True


def _handle_signal(signum, frame):
    global _running
    _running = False
    logger.info("Queue processor shutting down gracefully...")


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


async def main():
    """Queue processor main loop."""
    setup_logging()
    await init_database()
    logger.info("Queue processor started (%d sweep functions)", len(_sweep_functions))
    while _running:
        await run_sweep_cycle()
        await asyncio.sleep(5)
    logger.info("Queue processor stopped")


if __name__ == "__main__":
    asyncio.run(main())
