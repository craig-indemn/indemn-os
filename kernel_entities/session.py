"""Session — authentication state. Tokens, expiration, revocation."""

from datetime import datetime, timezone
from typing import Literal, Optional

from bson import ObjectId
from pydantic import Field

from kernel.entity.base import BaseEntity


class Session(BaseEntity):
    """Authentication state — tokens, expiration, revocation.

    Every authenticated identity has a Session. One validation path,
    one revocation mechanism, one audit trail.
    """

    actor_id: ObjectId
    type: Literal["user_interactive", "associate_service", "tier3_api", "cli_automation"]
    auth_method_used: str
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    status: Literal["active", "expired", "revoked"] = "active"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_active_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime
    access_token_jti: str
    refresh_token_ref: Optional[str] = None
    mfa_verified: bool = False
    mfa_verified_at: Optional[datetime] = None
    claims_stale: bool = False
    platform_admin_context: Optional[dict] = None

    _state_field_name = "status"
    _state_machine = {"active": ["expired", "revoked"]}
    _is_kernel_entity = True

    class Settings:
        name = "sessions"
        indexes = [
            [("actor_id", 1), ("status", 1)],
            [("access_token_jti", 1)],
            [("expires_at", 1)],
            [("org_id", 1), ("type", 1), ("status", 1)],
        ]
