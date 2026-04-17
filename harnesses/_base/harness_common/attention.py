"""Attention lifecycle helpers for real-time harnesses.

Used by chat and voice harnesses to track active sessions.
Attention = "this actor is currently attending to this entity."
"""

import asyncio
import logging

from .cli import indemn

log = logging.getLogger(__name__)


async def open_attention(
    actor_id: str,
    entity_type: str,
    entity_id: str,
    purpose: str = "real_time_session",
    runtime_id: str | None = None,
) -> dict:
    """Open an Attention record for a real-time session."""
    args = [
        "attention", "open",
        "--actor", actor_id,
        "--entity-type", entity_type,
        "--entity-id", entity_id,
        "--purpose", purpose,
    ]
    if runtime_id:
        args.extend(["--runtime", runtime_id])
    result = indemn(*args)
    log.info("Opened Attention: %s", result.get("_id"))
    return result


async def close_attention(attention_id: str) -> dict:
    """Close an Attention record."""
    try:
        result = indemn("attention", "close", attention_id)
        log.info("Closed Attention: %s", attention_id)
        return result
    except Exception as e:
        log.warning("Failed to close Attention %s: %s", attention_id, e)
        return {}


async def attention_heartbeat_loop(
    attention_id: str,
    interval_s: float = 30.0,
) -> None:
    """Send heartbeat to keep Attention alive. Runs until cancelled."""
    while True:
        try:
            indemn("attention", "heartbeat", attention_id)
        except Exception as e:
            log.warning("Attention heartbeat failed: %s", e)
        await asyncio.sleep(interval_s)
