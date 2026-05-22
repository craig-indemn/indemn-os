"""Checkpointer thread_id derivation per AI-404 design §13.3.

Real-time sessions use interaction_id (state accumulates across turns).
Async work uses target_entity_id if target is Interaction (multi-agent handoff
continuity), else message_id (per-invocation isolation in cascades).

LangSmith metadata.thread_id is ALWAYS correlation_id (the lineage tracker) —
this utility is ONLY for the LangGraph checkpointer's configurable.thread_id.

See operating-system/projects/customer-system/artifacts/
2026-05-18-deployment-runtime-harness-architecture.md §13 for full context.
"""

from typing import Protocol


class WorkContext(Protocol):
    """Either a real-time session context OR an async message context."""

    is_real_time_session: bool
    interaction_id: str | None
    target_entity_type: str | None
    target_entity_id: str | None
    message_id: str | None


def derive_checkpointer_thread_id(ctx: WorkContext) -> str:
    """Returns the LangGraph checkpointer thread_id per §13.3.

    The rule tracks the SUBJECT of the work:
    - Real-time session → interaction_id (state across turns within session)
    - Async targeting an Interaction → target_entity_id (handoff continuity)
    - Async other → message_id (per-invocation isolation)
    """
    if ctx.is_real_time_session:
        if not ctx.interaction_id:
            raise ValueError(
                "Real-time session work_context must have interaction_id"
            )
        return ctx.interaction_id

    if ctx.target_entity_type == "Interaction":
        if not ctx.target_entity_id:
            raise ValueError(
                "Async work targeting Interaction must have target_entity_id"
            )
        return ctx.target_entity_id

    if not ctx.message_id:
        raise ValueError("Async work must have message_id")
    return ctx.message_id
