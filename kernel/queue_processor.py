"""Queue processor — sweep process.

Runs extensible sweep functions each cycle.
Phase 1: visibility timeout recovery + escalation deadline checks.
Phase 2 adds: Temporal workflow dispatch for associate-eligible messages.
Phase 5 adds: Attention TTL cleanup + zombie detection.

Entry point: python -m kernel.queue_processor
"""

import asyncio
import logging
import signal
from datetime import datetime, timezone

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
