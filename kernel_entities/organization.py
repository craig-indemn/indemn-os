"""Organization — multi-tenancy scope. The boundary around everything."""

from typing import Literal, Optional

from pydantic import Field

from kernel.entity.base import BaseEntity


class Organization(BaseEntity):
    """Multi-tenancy scope. The boundary around everything."""

    name: str
    slug: str
    status: Literal["onboarding", "active", "suspended"] = "onboarding"
    settings: dict = Field(default_factory=dict)
    template_source: Optional[str] = None
    default_mfa_required: bool = False

    _state_machine = {
        "onboarding": ["active"],
        "active": ["suspended"],
        "suspended": ["active"],
    }
    _is_kernel_entity = True

    class Settings:
        name = "organizations"
        indexes = [[("slug", 1)]]
