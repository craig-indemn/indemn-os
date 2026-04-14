"""Skill document model.

Two kinds:
- Entity skills: auto-generated from entity definitions (reference material)
- Associate skills: authored by humans/AI (behavioral instructions)

Both stored in MongoDB, versioned, content-hashed for integrity.
"""

from datetime import datetime, timezone
from typing import Literal, Optional

from beanie import Document
from bson import ObjectId
from pydantic import Field


class Skill(Document):
    """Markdown document — entity skills (auto-generated) or associate skills (authored)."""

    org_id: Optional[ObjectId] = None  # None for system-level entity skills
    name: str
    type: Literal["entity", "associate"]
    entity_type: Optional[str] = None  # For entity skills: which entity
    content: str
    content_hash: str
    version: int = 1
    status: Literal["active", "pending_review", "deprecated"] = "active"
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"arbitrary_types_allowed": True}

    class Settings:
        name = "skills"
        indexes = [
            [("name", 1), ("status", 1)],
            [("org_id", 1), ("type", 1)],
        ]
