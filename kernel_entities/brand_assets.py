"""BrandAssets — reusable visual primitives shared across SurfaceConfigs.

A simple reference entity. No state-machine beyond active/archived. Used by
SurfaceConfigs that want to reference a shared brand (logo, colors, fonts)
rather than duplicating those fields per SurfaceConfig.

One BrandAssets record (e.g., "Indemn Brand") can be referenced by many
SurfaceConfigs (Indemn's chat config, Indemn's voice config, future
Indemn surfaces).

See docs/architecture/deployments.md § The Three Entities → BrandAssets.
"""

from typing import Literal, Optional

from kernel.entity.base import BaseEntity


class BrandAssets(BaseEntity):
    """Reusable visual primitives (logo, colors, fonts) shared across SurfaceConfigs."""

    name: str
    logo_url: str
    favicon_url: Optional[str] = None
    primary_color: str
    secondary_color: str
    accent_color: str
    font_family_heading: str
    font_family_body: str
    dark_mode_supported: bool = False  # §6.4 — per BrandAssets dark-mode capability
    status: Literal["active", "archived"] = "active"

    _state_field_name = "status"
    _state_machine = {"active": ["archived"], "archived": []}
    _is_kernel_entity = True

    class Settings:
        name = "brand_assets"
        indexes = [
            [("org_id", 1), ("name", 1)],
            [("org_id", 1), ("status", 1)],
        ]
