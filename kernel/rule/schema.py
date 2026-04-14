"""Rule and RuleGroup document models.

Rules are per-org condition→action patterns for deterministic entity processing.
Two actions only: set_fields and force_reasoning.
Rules are organized into groups with a lifecycle (draft → active → archived).
"""

from datetime import datetime, timezone
from typing import Literal, Optional

from beanie import Document
from bson import ObjectId
from pydantic import Field


class RuleGroup(Document):
    """Organizational container for related rules."""

    org_id: ObjectId
    entity_type: str
    name: str
    description: Optional[str] = None
    status: Literal["draft", "active", "archived"] = "draft"
    owner: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"arbitrary_types_allowed": True}

    class Settings:
        name = "rule_groups"
        indexes = [[("org_id", 1), ("entity_type", 1), ("status", 1)]]


class Rule(Document):
    """A condition→action pattern for deterministic entity processing.

    Two actions only:
    - set_fields: apply a deterministic result
    - force_reasoning: override and send to LLM (veto rule)
    """

    org_id: ObjectId
    entity_type: str
    capability: str  # auto_classify, auto_route, etc.
    group_id: Optional[ObjectId] = None  # Reference to RuleGroup
    name: Optional[str] = None
    conditions: dict  # JSON condition (same evaluator as watches)
    action: Literal["set_fields", "force_reasoning"]
    sets: Optional[dict] = None  # For set_fields: {field: value}
    forces_reasoning_reason: Optional[str] = None  # For force_reasoning
    priority: int = 100  # Higher = evaluated first
    status: Literal["draft", "active", "archived"] = "draft"
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"arbitrary_types_allowed": True}

    class Settings:
        name = "rules"
        indexes = [
            [("org_id", 1), ("entity_type", 1), ("capability", 1), ("status", 1), ("priority", -1)],
        ]
