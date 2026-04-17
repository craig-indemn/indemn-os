"""Actor — identity. Humans, AI associates, and developers as participants."""

from typing import Literal, Optional

from bson import ObjectId
from pydantic import Field

from kernel.entity.base import BaseEntity


class Actor(BaseEntity):
    """Identity — humans, AI associates, and developers as participants."""

    name: str
    email: Optional[str] = None
    type: Literal["human", "associate", "tier3_developer"]
    status: Literal["provisioned", "active", "suspended", "deprovisioned"] = "provisioned"
    role_ids: list[ObjectId] = Field(default_factory=list)

    # Associate-specific (None for humans)
    skills: Optional[list[str]] = None
    mode: Optional[Literal["deterministic", "reasoning", "hybrid"]] = None
    runtime_id: Optional[ObjectId] = None
    owner_actor_id: Optional[ObjectId] = None
    llm_config: Optional[dict] = None
    trigger_schedule: Optional[str] = None  # Cron expression for scheduled associates
    strict_deterministic: bool = False  # If true, RAISE on needs_reasoning instead of LLM fallback

    # Auth
    authentication_methods: list[dict] = Field(default_factory=list)
    mfa_exempt: bool = False

    _state_field_name = "status"
    _state_machine = {
        "provisioned": ["active"],
        "active": ["suspended", "deprovisioned"],
        "suspended": ["active", "deprovisioned"],
    }
    _is_kernel_entity = True

    class Settings:
        name = "actors"
        indexes = [
            [("org_id", 1), ("email", 1)],
            [("org_id", 1), ("type", 1), ("status", 1)],
            [("org_id", 1), ("role_ids", 1)],
        ]
