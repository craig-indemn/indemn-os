"""Integration — external connectivity. Provider, credentials, ownership, adapter dispatch."""

from datetime import datetime
from typing import Literal, Optional

from bson import ObjectId
from pydantic import Field

from kernel.entity.base import BaseEntity


class Integration(BaseEntity):
    """External connectivity — provider, credentials, ownership, adapter dispatch."""

    name: str
    owner_type: Literal["org", "actor"]
    owner_id: ObjectId
    system_type: str  # email, payment, voice, ams, carrier, identity_provider, etc.
    provider: str  # outlook, gmail, stripe, livekit, etc.
    provider_version: str = "v1"
    config: dict = Field(default_factory=dict)
    secret_ref: Optional[str] = None  # AWS Secrets Manager path
    access: Optional[dict] = None  # For org-level: {"roles": ["underwriter", "ops"]}
    status: Literal["configured", "connected", "active", "error", "paused"] = "configured"
    last_checked_at: Optional[datetime] = None
    last_error: Optional[str] = None
    content_visibility: Literal["full_shared", "metadata_shared", "owner_only"] = "full_shared"

    _state_field_name = "status"
    _state_machine = {
        "configured": ["connected"],
        "connected": ["active", "error"],
        "active": ["error", "paused", "configured"],
        "error": ["configured"],
        "paused": ["active", "configured"],
    }
    _is_kernel_entity = True

    class Settings:
        name = "integrations"
        indexes = [
            [("org_id", 1), ("system_type", 1), ("status", 1)],
            [("owner_type", 1), ("owner_id", 1)],
        ]
