"""Attention — active working context. Who is attending to what, right now."""

from datetime import datetime, timezone
from typing import Literal, Optional

from bson import ObjectId
from pydantic import Field

from kernel.entity.base import BaseEntity


class Attention(BaseEntity):
    """Active working context — who is attending to what, right now.

    Unifies: UI soft-locks, real-time session tracking, and scoped event routing.
    Heartbeat-maintained with TTL expiration.
    """

    actor_id: ObjectId
    target_entity: dict  # {"type": "Interaction", "id": ObjectId}
    related_entities: list[dict] = Field(default_factory=list)

    purpose: Literal[
        "real_time_session",
        "observing",
        "review",
        "editing",
        "claim_in_progress",
    ]

    runtime_id: Optional[ObjectId] = None
    workflow_id: Optional[str] = None
    session_id: Optional[str] = None

    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_heartbeat: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime

    metadata: dict = Field(default_factory=dict)
    status: Literal["active", "expired", "closed"] = "active"

    _state_machine = {"active": ["expired", "closed"]}
    _is_kernel_entity = True

    class Settings:
        name = "attentions"
        indexes = [
            [("actor_id", 1), ("purpose", 1)],
            [("target_entity.id", 1)],
            [("related_entities.id", 1)],
            [("runtime_id", 1), ("purpose", 1)],
            [("expires_at", 1)],
            [("org_id", 1), ("status", 1)],
        ]
