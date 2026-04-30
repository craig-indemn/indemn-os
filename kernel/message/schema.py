"""Message and MessageLog document models.

Messages are the nervous system. Generated when entities change, routed to actors.
Split storage: message_queue (hot, active) and message_log (cold, completed).
"""

from datetime import datetime, timezone
from typing import Literal, Optional

from beanie import Document
from bson import ObjectId
from pydantic import Field


class Message(Document):
    """Active message in the queue."""

    org_id: ObjectId
    entity_type: str
    entity_id: ObjectId
    event_type: str

    target_role: str
    target_actor_id: Optional[ObjectId] = None

    correlation_id: str
    causation_id: Optional[str] = None
    depth: int = 0

    status: Literal[
        "pending",
        "processing",
        "completed",
        "failed",
        "dead_letter",
        "circuit_broken",
        # Bug #38 root cause #3: target role has type=associate actors but
        # none in status=active. Park the message; the dispatch sweep
        # re-evaluates it next cycle (and dispatches when the actor is
        # reactivated). Distinct from `pending` so log spam stays bounded
        # and operators can see the parked tail in queue stats.
        "parked",
    ] = "pending"
    claimed_by: Optional[ObjectId] = None
    claimed_at: Optional[datetime] = None
    visibility_timeout: Optional[datetime] = None
    attempt_count: int = 0
    max_attempts: int = 3

    priority: Literal["critical", "high", "normal", "low"] = "normal"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    due_by: Optional[datetime] = None

    event_metadata: dict = Field(default_factory=dict)
    context: dict = Field(default_factory=dict)  # Enriched entity context
    summary: dict = Field(default_factory=dict)

    last_error: Optional[str] = None

    model_config = {"arbitrary_types_allowed": True}

    class Settings:
        name = "message_queue"
        indexes = [
            [("org_id", 1), ("target_role", 1), ("status", 1), ("priority", -1), ("created_at", 1)],
            [("status", 1), ("visibility_timeout", 1)],
            [("correlation_id", 1), ("created_at", 1)],
        ]


class MessageLog(Document):
    """Completed messages (cold storage)."""

    org_id: ObjectId
    entity_type: str
    entity_id: ObjectId
    event_type: str
    target_role: str
    target_actor_id: Optional[ObjectId] = None
    correlation_id: str
    causation_id: Optional[str] = None
    depth: int = 0
    claimed_by: Optional[ObjectId] = None
    claimed_at: Optional[datetime] = None
    priority: str = "normal"
    created_at: datetime
    event_metadata: dict = Field(default_factory=dict)
    result: Optional[dict] = None
    completed_at: Optional[datetime] = None

    model_config = {"arbitrary_types_allowed": True}

    class Settings:
        name = "message_log"
        indexes = [
            [("org_id", 1), ("entity_type", 1), ("entity_id", 1), ("created_at", -1)],
            [("correlation_id", 1), ("created_at", 1)],
            [("org_id", 1), ("created_at", -1)],
        ]
