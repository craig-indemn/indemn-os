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
    Return them to pending status for re-claiming."""
    now = datetime.now(timezone.utc)
    result = await Message.get_motor_collection().update_many(
        {
            "status": "processing",
            "visibility_timeout": {"$lt": now},
        },
        {
            "$set": {"status": "pending", "claimed_by": None, "visibility_timeout": None},
        },
    )
    if result.modified_count > 0:
        logger.info("Recovered %d timed-out messages", result.modified_count)


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


async def dispatch_associate_workflows():
    """Find pending messages for roles with associates.
    Start Temporal workflows for them.

    This is the SWEEP BACKSTOP — optimistic dispatch from the API
    is the primary path. This catches anything that was missed.
    """
    from kernel.temporal.client import get_temporal_client
    from kernel.temporal.workflows import ProcessMessageWorkflow
    from kernel_entities.actor import Actor
    from kernel_entities.role import Role

    client = await get_temporal_client()
    if not client:
        return  # Temporal not configured

    # Find pending messages older than the dispatch threshold
    threshold = datetime.now(timezone.utc) - timedelta(seconds=10)
    messages = await Message.find({
        "status": "pending",
        "created_at": {"$lt": threshold},
    }).to_list(length=100)

    if not messages:
        return

    for message in messages:
        try:
            # Look up role by name + org [G-66]
            role = await Role.find_one({
                "name": message.target_role,
                "org_id": message.org_id,
            })
            if not role:
                continue

            # Check if this role has active associate actors
            associates = await Actor.find({
                "type": "associate",
                "role_ids": role.id,
                "status": "active",
                "org_id": message.org_id,
            }).to_list()

            try:
                from temporalio.client import WorkflowAlreadyStartedError

                if associates:
                    # Associate available — ProcessMessageWorkflow
                    associate = associates[0]
                    await client.start_workflow(
                        ProcessMessageWorkflow.run,
                        args=[str(message.id), str(associate.id)],
                        id=f"msg-{message.id}",
                        task_queue="indemn-kernel",
                    )
                else:
                    # No associates — route to HumanReviewWorkflow
                    from kernel.temporal.workflows import HumanReviewWorkflow

                    await client.start_workflow(
                        HumanReviewWorkflow.run,
                        args=[str(message.id)],
                        id=f"human-review-{message.id}",
                        task_queue="indemn-kernel",
                    )
            except WorkflowAlreadyStartedError:
                pass  # Already dispatched — optimistic dispatch got it
            except Exception as e:
                logger.warning("Failed to dispatch workflow %s: %s", message.id, e)

        except Exception as e:
            logger.error("Error dispatching message %s: %s", message.id, e)


async def check_scheduled_associates():
    """Check for associates with schedule triggers whose cron has fired. [G-76]"""
    from croniter import croniter

    from kernel_entities.actor import Actor
    from kernel_entities.role import Role

    associates = await Actor.find({
        "type": "associate",
        "status": "active",
        "trigger_schedule": {"$exists": True, "$ne": None},
    }).to_list()

    now = datetime.now(timezone.utc)

    for associate in associates:
        try:
            cron = croniter(associate.trigger_schedule, now - timedelta(minutes=1))
            next_fire = cron.get_next(datetime)
            if next_fire <= now:
                # Check if we already created a message for this firing
                existing = await Message.find_one({
                    "entity_type": "_scheduled",
                    "entity_id": associate.id,
                    "created_at": {"$gte": now - timedelta(minutes=1)},
                })
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
