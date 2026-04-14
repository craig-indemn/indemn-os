"""Optimistic dispatch — fire-and-forget Temporal workflow start.

Called AFTER the MongoDB transaction commits (not inside it).
If this fails, the queue processor sweep catches it.

Phase 1: no-op (Temporal not used yet).
Phase 2: starts ProcessMessageWorkflow for associate-eligible messages.
"""

from kernel.message.schema import Message


async def optimistic_dispatch(messages: list[Message]):
    """Fire-and-forget Temporal workflow start for associate-eligible messages.

    Phase 1: no-op. Phase 2 activates this with Temporal workflow starts.
    """
    # Phase 2 implementation:
    # 1. Look up role for each message
    # 2. Check if role has active associate actors
    # 3. Start ProcessMessageWorkflow via Temporal client
    # 4. Fire and forget — sweep catches failures
    pass
