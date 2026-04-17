"""Interaction lifecycle helpers for real-time harnesses.

Used by chat and voice harnesses to manage conversation sessions.
"""

import logging

from .cli import indemn

log = logging.getLogger(__name__)


async def create_interaction(
    channel_type: str,
    associate_id: str,
    deployment_id: str | None = None,
) -> dict:
    """Create an Interaction entity for a new conversation session."""
    data = {
        "channel_type": channel_type,
        "status": "active",
        "handling_actor_id": associate_id,
    }
    if deployment_id:
        data["deployment_id"] = deployment_id

    # Use the CLI to create — goes through the auto-generated CRUD endpoint
    import json
    result = indemn(
        "interaction", "create",
        "--channel-type", channel_type,
        "--associate", associate_id,
        *(["--deployment", deployment_id] if deployment_id else []),
    )
    log.info("Created Interaction: %s", result.get("_id"))
    return result


async def close_interaction(interaction_id: str) -> dict:
    """Close an Interaction (transition to closed)."""
    try:
        result = indemn("interaction", "close", interaction_id)
        log.info("Closed Interaction: %s", interaction_id)
        return result
    except Exception as e:
        log.warning("Failed to close Interaction %s: %s", interaction_id, e)
        return {}


async def transfer_interaction(
    interaction_id: str,
    to_actor: str | None = None,
    to_role: str | None = None,
) -> dict:
    """Transfer an Interaction to another actor or role (handoff)."""
    args = ["interaction", "transfer", interaction_id]
    if to_actor:
        args.extend(["--to-actor", to_actor])
    if to_role:
        args.extend(["--to-role", to_role])
    result = indemn(*args)
    log.info("Transferred Interaction %s", interaction_id)
    return result
