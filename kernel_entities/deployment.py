"""Deployment — placement of an associate on a specific surface (a venue).

A Deployment binds an Associate to a Runtime (which determines the channel), points at a
SurfaceConfig (which configures the visual presentation), and carries the per-venue
configuration (greeting, parameter contract, LLM override, acts_as auth identity policy).

One Associate → many Deployments (the "one associate, many venues" pattern). Same agent,
different placements with different visual configuration, different initialization
parameters, different greeting, optionally different LLM overrides, optionally different
auth identity model.

See docs/architecture/deployments.md for the full design.
"""

from typing import Literal, Optional

from bson import ObjectId
from pydantic import Field
from pymongo import ASCENDING, IndexModel

from kernel.entity.base import BaseEntity


class Deployment(BaseEntity):
    """One specific placement of an associate, with surface-specific config."""

    name: str
    associate_id: ObjectId
    runtime_id: ObjectId
    surface_config_id: Optional[ObjectId] = None

    parameter_schema: dict = Field(default_factory=dict)
    static_parameters: dict = Field(default_factory=dict)
    # parameter_schema_validation_mode: per §5.4, default DERIVED from acts_as
    # (strict for session_actor, forgiving for associate_self). Derivation
    # happens in the `_derive_validation_mode` model_validator added in Task 1.2.
    # Field is Optional[Literal] so the validator can fill it.
    parameter_schema_validation_mode: Optional[Literal["strict", "forgiving"]] = None

    llm_override: dict = Field(default_factory=dict)
    greeting: str = ""

    # acts_as: per §5.6, default DERIVED from parameter_schema. Stored
    # explicitly (not lazily recomputed) — see _derive_acts_as_and_validate
    # in Task 1.2. Field is Optional[Literal] so the validator can fill it.
    acts_as: Optional[Literal["session_actor", "associate_self"]] = None

    allowed_origins: list[str] = Field(default_factory=list)
    resumption_config: dict = Field(
        default_factory=lambda: {"ttl_seconds": 86400, "kill_on_resume": True}
    )

    status: Literal["configured", "active", "paused", "error", "archived"] = "configured"

    _state_field_name = "status"
    _state_machine = {
        "configured": ["active", "error"],
        "active": ["paused", "error", "archived"],
        "paused": ["active", "archived"],
        "error": ["configured"],
        "archived": [],
    }
    _is_kernel_entity = True

    class Settings:
        name = "deployments"
        indexes = [
            # (org_id, name) is unique within an org — operator-friendly lookup
            # by name + prevents accidental duplicate Deployments. Beanie's
            # default index list shape uses tuples; uniqueness requires the
            # full IndexModel spec.
            IndexModel(
                [("org_id", ASCENDING), ("name", ASCENDING)],
                unique=True,
            ),
            IndexModel(
                [("org_id", ASCENDING), ("associate_id", ASCENDING), ("status", ASCENDING)]
            ),
            IndexModel(
                [("org_id", ASCENDING), ("runtime_id", ASCENDING), ("status", ASCENDING)]
            ),
            IndexModel([("org_id", ASCENDING), ("status", ASCENDING)]),
        ]
