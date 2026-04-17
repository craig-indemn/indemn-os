"""Runtime instance lifecycle — register + heartbeat."""

import asyncio
import logging
import os

from .cli import indemn

RUNTIME_ID = os.environ.get("RUNTIME_ID", "")
log = logging.getLogger(__name__)

MAX_CONSECUTIVE_HEARTBEAT_FAILURES = 10


async def register_instance() -> dict:
    """Call at harness startup."""
    result = indemn("runtime", "register-instance", "--runtime-id", RUNTIME_ID)
    log.info("Registered instance: %s", result.get("instance_id"))
    return result


async def heartbeat_loop(interval_s: float = 30.0) -> None:
    consecutive_failures = 0
    while True:
        try:
            indemn("runtime", "heartbeat", "--runtime-id", RUNTIME_ID)
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            log.warning("Heartbeat failed (%d/%d): %s",
                        consecutive_failures, MAX_CONSECUTIVE_HEARTBEAT_FAILURES, e)
            if consecutive_failures >= MAX_CONSECUTIVE_HEARTBEAT_FAILURES:
                log.error("Heartbeat failed %d consecutive times — exiting",
                          MAX_CONSECUTIVE_HEARTBEAT_FAILURES)
                raise SystemExit(1)
        await asyncio.sleep(interval_s)
