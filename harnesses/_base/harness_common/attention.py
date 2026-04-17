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
    """Open an Attention record for a real-time session.

    Creates directly via API (not CLI) because Attention needs computed
    fields (expires_at, opened_at, last_heartbeat) set at creation.
    """
    from datetime import datetime, timedelta, timezone
    import json as json_mod

    from .cli import CLIError

    # Use httpx directly — Attention has required fields the CLI wrapper doesn't handle
    import os
    import httpx

    base_url = os.environ["INDEMN_API_URL"]
    token = os.environ.get("INDEMN_SERVICE_TOKEN", "")
    now = datetime.now(timezone.utc)

    data = {
        "actor_id": actor_id,
        "target_entity": {"type": entity_type, "id": entity_id},
        "purpose": purpose,
        "opened_at": now.isoformat(),
        "last_heartbeat": now.isoformat(),
        "expires_at": (now + timedelta(minutes=2)).isoformat(),
    }
    if runtime_id:
        data["runtime_id"] = runtime_id

    with httpx.Client(base_url=base_url, timeout=30) as client:
        r = client.post(
            "/api/attentions/",
            json=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        if r.status_code >= 400:
            raise CLIError(f"Attention create failed ({r.status_code}): {r.text[:500]}")
        result = r.json()

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
