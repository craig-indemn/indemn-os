"""Shared Pydantic models crossing kernel <-> harness boundaries."""

from typing import Optional

from pydantic import BaseModel


class AgentExecutionInput(BaseModel):
    """Typed input for process_with_associate activity.

    Used by both kernel ProcessMessageWorkflow (caller) and harness activity (callee).
    Adding optional fields is non-breaking.
    """

    message_id: str
    associate_id: str
    entity_type: str
    entity_id: str
    correlation_id: str
    depth: int
    parent_message_id: Optional[str] = None
    trace_context: Optional[dict] = None


class AgentExecutionResult(BaseModel):
    """Result returned by process_with_associate activity."""

    status: str  # "complete" | "failed"
    iterations: int
    tools_used: list[str]
    error: Optional[str] = None
