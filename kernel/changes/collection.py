"""Changes collection — append-only audit trail.

Every entity mutation is recorded with field-level detail: who changed what,
when, from what value, to what value, and why. A sequential hash chain links
records together for tamper evidence.

MongoDB permission: INSERT only. No UPDATE, no DELETE.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from beanie import Document
from bson import ObjectId
from pydantic import BaseModel, Field


class FieldChange(BaseModel):
    """A single field-level change."""

    field: str
    old_value: Optional[Any] = None
    new_value: Optional[Any] = None


class ChangeRecord(Document):
    """Append-only audit trail. Every entity mutation recorded."""

    org_id: ObjectId
    entity_type: str
    entity_id: ObjectId
    change_type: str  # create, update, delete, transition, auth.*
    actor_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: Optional[str] = None
    changes: list[FieldChange] = Field(default_factory=list)
    method: Optional[str] = None
    method_metadata: Optional[dict] = None  # Rule evaluation results go here
    previous_hash: Optional[str] = None
    current_hash: Optional[str] = None

    model_config = {"arbitrary_types_allowed": True}

    class Settings:
        name = "changes"
        indexes = [
            [("org_id", 1), ("entity_type", 1), ("entity_id", 1), ("timestamp", -1)],
            [("org_id", 1), ("timestamp", -1)],
            [("correlation_id", 1)],
            [("org_id", 1), ("actor_id", 1), ("timestamp", -1)],
        ]


async def write_change_record(
    entity,
    change_type: str,
    actor_id: str,
    changes: list[dict],
    method: Optional[str],
    method_metadata: Optional[dict],
    correlation_id: Optional[str],
    session=None,
):
    """Write a change record within the entity save transaction."""
    from kernel.changes.hash_chain import compute_hash, get_previous_hash

    record = ChangeRecord(
        org_id=entity.org_id,
        entity_type=type(entity).__name__,
        entity_id=entity.id,
        change_type=change_type,
        actor_id=actor_id,
        correlation_id=correlation_id,
        changes=[FieldChange(**c) for c in changes],
        method=method,
        method_metadata=method_metadata,
    )

    # Hash chain
    record.previous_hash = await get_previous_hash(entity.org_id, session)
    record.current_hash = compute_hash(record)

    await record.insert(session=session)
