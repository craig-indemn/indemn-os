"""Utilities for idempotent message processing.

Helpers for actors to check "have I already processed this?" before
taking non-idempotent actions (sending emails, creating entities).

For MVP: in-memory cache (single worker). For scale: MongoDB collection with TTL index.
"""

_processed_cache: dict[str, bool] = {}


async def check_already_processed(message_id: str) -> bool:
    """Check if a message has already been processed."""
    return message_id in _processed_cache


async def mark_processed(message_id: str):
    """Mark a message as processed."""
    _processed_cache[message_id] = True
