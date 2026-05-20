"""Unit tests for SurfaceConfig kernel entity (AI-404-1).

Class-attribute introspection per the test_trace_entity.py convention.
Beanie Document subclasses can't be instantiated without init_beanie();
construction-level behavior is verified later by integration tests
(Task 1.10.5+ fixtures + Task 1.11 endpoint tests).
"""

from typing import get_args

from kernel_entities.surface_config import SurfaceConfig


def test_surface_config_can_import():
    """SurfaceConfig class can be imported."""
    assert SurfaceConfig is not None


def test_surface_config_required_fields():
    """name, channel_kind, vendor are required (§6.3)."""
    for field_name in ("name", "channel_kind", "vendor"):
        assert SurfaceConfig.model_fields[field_name].is_required(), (
            f"{field_name} should be required"
        )


def test_surface_config_status_default():
    """status defaults to 'configured'."""
    assert SurfaceConfig.model_fields["status"].default == "configured"


def test_surface_config_channel_kind_literal_values():
    """channel_kind enumerates the 6 supported channels per §6.3."""
    annotation = SurfaceConfig.model_fields["channel_kind"].annotation
    args = get_args(annotation)
    assert set(args) == {"chat", "voice", "slack", "email", "teams", "sms"}


def test_surface_config_status_state_machine():
    """configured → active → archived; active also goes directly to archived
    (covers the never-deployed-then-retired case)."""
    assert SurfaceConfig._state_machine == {
        "configured": ["active", "archived"],
        "active": ["archived"],
        "archived": [],
    }


def test_surface_config_is_kernel_entity_marker():
    """SurfaceConfig is marked as a kernel entity."""
    assert SurfaceConfig._is_kernel_entity is True


def test_surface_config_settings_collection_name():
    """Beanie Settings.name is 'surface_configs' (auto-route /api/surface_configs/)."""
    assert SurfaceConfig.Settings.name == "surface_configs"
