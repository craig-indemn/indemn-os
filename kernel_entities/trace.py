"""Trace — durable LLM execution record for every associate run.

The third leg of the OS's observability triad:
- Changes collection: entity mutations
- Message log: work completed
- Trace: LLM reasoning

Kernel entity because it's universal across all orgs, enables the
evaluator to be a standard associate via watch→message→workflow,
and fills the durability gap for LLM data (LangSmith deletes after
14 days).
"""

from datetime import datetime
from typing import Literal, Optional

from bson import ObjectId
from pydantic import Field

from kernel.entity.base import BaseEntity


class Trace(BaseEntity):
    """Durable LLM execution record for one associate run."""

    # Identity (mirrors LangSmith Run)
    trace_id: Optional[str] = None
    langsmith_run_id: Optional[str] = None
    session_id: Optional[str] = None

    # OS linking (extends LangSmith)
    associate_id: ObjectId
    associate_name: str
    message_id: ObjectId
    correlation_id: Optional[str] = None
    entity_type: str
    entity_id: ObjectId

    # Execution record
    name: Optional[str] = None
    run_type: str = "chain"
    inputs: dict = Field(default_factory=dict)
    outputs: dict = Field(default_factory=dict)
    messages: list[dict] = Field(default_factory=list)
    child_runs: list[dict] = Field(default_factory=list)
    events: list[dict] = Field(default_factory=list)

    # Metadata
    tags: list[str] = Field(default_factory=list)
    extra: dict = Field(default_factory=dict)

    # Metrics
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    total_cost: Optional[float] = None
    prompt_cost: Optional[float] = None
    completion_cost: Optional[float] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_ms: Optional[int] = None
    first_token_time: Optional[datetime] = None

    # Execution outcome (mirrors LangSmith Run status)
    execution_status: Literal["success", "error", "cancelled"] = "success"
    error: Optional[str] = None

    # Evaluation lifecycle (OS state machine — separate from execution_status)
    status: Literal["created", "evaluated"] = "created"
    feedback_stats: dict = Field(default_factory=dict)

    _state_field_name = "status"
    _state_machine = {"created": ["evaluated"]}
    _is_kernel_entity = True

    class Settings:
        name = "traces"
        indexes = [
            [("org_id", 1), ("associate_id", 1), ("created_at", -1)],
            [("org_id", 1), ("entity_type", 1), ("entity_id", 1)],
            [("org_id", 1), ("correlation_id", 1)],
            [("org_id", 1), ("status", 1)],
            [("org_id", 1), ("execution_status", 1)],
        ]
