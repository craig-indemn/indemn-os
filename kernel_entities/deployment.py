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

from typing import Literal, Optional, Self

from bson import ObjectId
from pydantic import Field, model_validator
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

    @model_validator(mode="after")
    def _derive_acts_as(self) -> Self:
        """Derive acts_as from parameter_schema if not supplied (design doc §5.1 + §5.6).

        Rule: if parameter_schema lists actor_id in required, default to
        session_actor. Otherwise default to associate_self. Derivation runs
        once at construction time; the value is stored explicitly on the
        record (not lazily recomputed).
        """
        if self.acts_as is None:
            required_fields = self.parameter_schema.get("required", [])
            if "actor_id" in required_fields:
                self.acts_as = "session_actor"
            else:
                self.acts_as = "associate_self"
        return self

    @model_validator(mode="after")
    def _derive_validation_mode(self) -> Self:
        """Derive parameter_schema_validation_mode from acts_as if not supplied (§5.4).

        Rule (validation failure policy):
        - session_actor (internal Deployments) → "strict" — reject the
          connection on dynamic_params validation error
        - associate_self (public Deployments) → "forgiving" — open the
          session with validation_warnings; agent decides whether to continue
        """
        if self.parameter_schema_validation_mode is None:
            self.parameter_schema_validation_mode = (
                "strict" if self.acts_as == "session_actor" else "forgiving"
            )
        return self

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
