"""Role — permissions and watches. What actors can do and what flows to them."""

from typing import Optional

from bson import ObjectId
from pydantic import BaseModel, Field

from kernel.entity.base import BaseEntity


class WatchDefinition(BaseModel):
    """A watch on a role — declares what entity changes matter to actors in this role."""

    entity_type: str
    event: str  # "created", "transitioned", "method_invoked", "fields_changed", "deleted"
    conditions: Optional[dict] = None  # JSON condition (same language as rules)
    scope: Optional[dict] = None  # field_path or active_context scope
    context_depth: int = 1  # How deep to resolve related entities in message context


class Role(BaseEntity):
    """Permissions and watches — what actors can do and what flows to them."""

    name: str
    permissions: dict = Field(default_factory=dict)
    # Format: {"read": ["Submission", "Email"], "write": ["Submission", "Draft"]}
    # "*" means all entity types

    watches: list[WatchDefinition] = Field(default_factory=list)
    can_grant: Optional[list[str]] = None  # Role names this role can grant to others
    mfa_required: bool = False
    is_inline: bool = False  # True for associate-specific singleton roles
    bound_actor_id: Optional[ObjectId] = None  # Set for inline roles

    _is_kernel_entity = True

    class Settings:
        name = "roles"
        indexes = [[("org_id", 1), ("name", 1)]]
