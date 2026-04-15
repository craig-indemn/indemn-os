"""Attention — active working context. Who is attending to what, right now."""

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from bson import ObjectId
from pydantic import Field

from kernel.entity.base import BaseEntity
from kernel.entity.exposed import exposed


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

    @exposed
    async def heartbeat(self):
        """Update heartbeat timestamp and extend TTL. [G-43]

        Called by harnesses every 30 seconds to keep the Attention alive.
        The save_tracked fast-path detects heartbeat-only changes and
        skips the changes collection + watches for performance.
        """
        self.last_heartbeat = datetime.now(timezone.utc)
        self.expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
        await self.save_tracked(
            actor_id=f"system:heartbeat:{self.actor_id}",
            method="heartbeat",
        )
        return {"status": "ok", "expires_at": self.expires_at.isoformat()}

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
