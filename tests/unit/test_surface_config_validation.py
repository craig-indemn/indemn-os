"""Tests for per-vendor JSON Schema validation of SurfaceConfig.config (AI-404-1).

Schema file existence tests use the file-system directly. Validation behavior
tests use Pydantic's model_construct + manual validator invocation per the
A.2 testing pattern established in test_deployment_entity.py.
"""

from pathlib import Path

import pytest
from bson import ObjectId

from kernel_entities.surface_config import SurfaceConfig

# --- Smoke test: PR #1's schema files are on disk ----------------------
# Without these files, every validation test would fail with the same
# opaque error. Run these first; if they fail, Pre-flight 4 wasn't completed.


def test_prompt_kit_schema_exists():
    """schemas/surface_configs/prompt-kit.schema.json must be on disk (PR #1)."""
    repo_root = Path(__file__).parent.parent.parent
    schema = repo_root / "schemas" / "surface_configs" / "prompt-kit.schema.json"
    assert schema.exists(), (
        f"Missing {schema} — required by SurfaceConfig.config validation. "
        f"Pre-flight 4 (PR #1 merge) must complete before Task 1.8."
    )


def test_livekit_schema_exists():
    """schemas/surface_configs/livekit.schema.json must be on disk (PR #1)."""
    repo_root = Path(__file__).parent.parent.parent
    schema = repo_root / "schemas" / "surface_configs" / "livekit.schema.json"
    assert schema.exists(), (
        f"Missing {schema} — required by SurfaceConfig.config validation. "
        f"Pre-flight 4 (PR #1 merge) must complete before Task 1.8."
    )


# --- Validation behavior: A.2 pattern (model_construct + manual call) -----


def _make_surface_config(**overrides):
    """Build a SurfaceConfig via model_construct (skips Document.__init__).
    Tests then invoke `_validate_config_against_vendor_schema()` to exercise
    the validator under test."""
    defaults = dict(
        org_id=ObjectId(),
        name="Test",
        channel_kind="chat",
        vendor="prompt-kit",
        config={},
    )
    defaults.update(overrides)
    return SurfaceConfig.model_construct(**defaults)


def test_valid_promptkit_config_accepted():
    """prompt-kit config with valid widget_position passes validation."""
    sc = _make_surface_config(
        vendor="prompt-kit",
        config={
            "widget_position": "bottom-right",
            "show_header": True,
            "header_text": "Hi",
        },
    )
    sc._validate_config_against_vendor_schema()  # should NOT raise
    assert sc.config["widget_position"] == "bottom-right"


def test_invalid_promptkit_config_rejected():
    """widget_position value outside enum → rejected with the offending field name."""
    sc = _make_surface_config(
        vendor="prompt-kit",
        config={"widget_position": "not-a-valid-position"},
    )
    with pytest.raises(ValueError, match="widget_position"):
        sc._validate_config_against_vendor_schema()


def test_missing_required_field_rejected():
    """prompt-kit schema requires widget_position; missing → rejected."""
    sc = _make_surface_config(
        vendor="prompt-kit",
        config={"show_header": True},
    )
    with pytest.raises(ValueError, match="widget_position"):
        sc._validate_config_against_vendor_schema()


def test_unknown_vendor_rejected():
    """Vendor with no schema file → ValueError wrapping FileNotFoundError."""
    sc = _make_surface_config(
        vendor="totally-unknown-vendor",
        config={},
    )
    with pytest.raises(ValueError, match="(?i)schema"):
        sc._validate_config_against_vendor_schema()


def test_livekit_config_valid():
    """LiveKit config with required stt_provider + tts_provider passes."""
    sc = _make_surface_config(
        channel_kind="voice",
        vendor="livekit",
        config={
            "stt_provider": "deepgram",
            "tts_provider": "cartesia",
        },
    )
    sc._validate_config_against_vendor_schema()
    assert sc.config["stt_provider"] == "deepgram"
