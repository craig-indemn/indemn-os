"""Unit tests for BrandAssets kernel entity (AI-404-1).

Class-attribute introspection per test_trace_entity.py convention.
"""

from typing import get_args

from kernel_entities.brand_assets import BrandAssets


def test_brand_assets_can_import():
    """BrandAssets class can be imported."""
    assert BrandAssets is not None


def test_brand_assets_required_fields():
    """name, logo_url, primary/secondary/accent colors, font_family_* required per §6.4."""
    required = (
        "name",
        "logo_url",
        "primary_color",
        "secondary_color",
        "accent_color",
        "font_family_heading",
        "font_family_body",
    )
    for field_name in required:
        assert BrandAssets.model_fields[field_name].is_required(), (
            f"{field_name} should be required"
        )


def test_brand_assets_status_default_active():
    """BrandAssets has simpler lifecycle than Deployment — default 'active'
    (no configured stage; brand assets are immediately usable)."""
    assert BrandAssets.model_fields["status"].default == "active"


def test_brand_assets_status_simple_lifecycle():
    """active → archived only (no configured stage per §6.4 simplicity)."""
    assert BrandAssets._state_machine == {"active": ["archived"], "archived": []}


def test_brand_assets_dark_mode_supported_default_false():
    """§6.4 dark_mode_supported flag exists, defaults False, typed bool."""
    field = BrandAssets.model_fields["dark_mode_supported"]
    assert field.default is False
    assert field.annotation is bool


def test_brand_assets_favicon_url_optional():
    """favicon_url is Optional[str] (logo is required; favicon nice-to-have)."""
    annotation = BrandAssets.model_fields["favicon_url"].annotation
    args = get_args(annotation)
    assert type(None) in args
    assert str in args


def test_brand_assets_is_kernel_entity_marker():
    """BrandAssets is marked as a kernel entity."""
    assert BrandAssets._is_kernel_entity is True


def test_brand_assets_settings_collection_name():
    """Beanie Settings.name is 'brand_assets'."""
    assert BrandAssets.Settings.name == "brand_assets"
