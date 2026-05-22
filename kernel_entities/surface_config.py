"""SurfaceConfig — visual + vendor configuration for a Deployment's UI.

Per-vendor-and-channel. Same Deployment using prompt-kit on a chat widget and LiveKit
on a voice widget needs two SurfaceConfigs (different vendors, different config shapes).
Same brand on Branch's portal and GIC's portal would use two SurfaceConfigs but
reference the same shared BrandAssets.

The `config` field is validated against the per-vendor JSON Schema file at
`indemn-os/schemas/surface_configs/{vendor}.schema.json` — see Task 1.8 for the
validation hook wiring.

See docs/architecture/deployments.md § The Three Entities → SurfaceConfig.
"""

from typing import Literal, Optional, Self

import jsonschema
from bson import ObjectId
from pydantic import Field, model_validator

from kernel.entity.base import BaseEntity
from kernel.schema_validation import validate_surface_config


class SurfaceConfig(BaseEntity):
    """Visual + vendor configuration for a Deployment's UI."""

    name: str
    channel_kind: Literal["chat", "voice", "slack", "email", "teams", "sms"]
    vendor: str  # "prompt-kit", "livekit", "slack-api", "gmail", "msteams", ...
    config: dict = Field(default_factory=dict)
    brand_assets_id: Optional[ObjectId] = None
    status: Literal["configured", "active", "archived"] = "configured"

    _state_field_name = "status"
    _state_machine = {
        "configured": ["active", "archived"],
        "active": ["archived"],
        "archived": [],
    }
    _is_kernel_entity = True

    @model_validator(mode="after")
    def _validate_config_against_vendor_schema(self) -> Self:
        """Validate self.config against the per-vendor JSON Schema file (§6).

        Schema files live at indemn-os/schemas/surface_configs/{vendor}.schema.json.
        Adding a new vendor = adding a new schema file (no Python class change).

        Raises:
            ValueError (Pydantic surfaces as ValidationError) on:
                - unknown vendor (no schema file on disk)
                - config doesn't satisfy the vendor schema
        """
        try:
            validate_surface_config(self.vendor, self.config)
        except FileNotFoundError as e:
            raise ValueError(str(e)) from e
        except jsonschema.ValidationError as e:
            raise ValueError(
                f"SurfaceConfig.config failed schema validation for vendor "
                f"'{self.vendor}': {e.message} at path {list(e.absolute_path)}"
            ) from e
        return self

    class Settings:
        name = "surface_configs"
        indexes = [
            [("org_id", 1), ("name", 1)],
            [("org_id", 1), ("vendor", 1)],
            [("org_id", 1), ("channel_kind", 1), ("status", 1)],
        ]
