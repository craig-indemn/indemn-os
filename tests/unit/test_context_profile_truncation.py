"""Tests for `kernel/api/context_profile.py` + `serialize_for_profile`.

Per-field truncation policy moved from harness to kernel. The harness's
old `_FIELD_TRUNCATE_LIMIT = 20_000` was a hardcoded entity-agnostic cap.
Now per-field `content_size_hint` on FieldDefinition drives a profile-based
cap applied at response-serialization time.

These tests pin:
- The PROFILE_CAPS table shape (raw uncapped, llm capped per hint).
- `apply_cap` slicing math (returned bytes ≤ cap, marker subtracted).
- `serialize_for_profile` short-circuits for kernel entities (no
  `_field_definitions` → no caps).
- `serialize_for_profile` looks up hints from
  `entity_cls._field_definitions[fname].content_size_hint`.
- The truncation marker points to `?context_profile=raw` as the escape
  hatch (so the agent has a real URL to fetch full content).
- The default profile is `raw` (no behavior change for existing callers).
"""

import inspect

import pytest

import kernel.api.context_profile as context_profile_module
import kernel.api.serialize as serialize_module
from kernel.api.context_profile import (
    PROFILE_CAPS,
    TRUNCATION_MARKER_TEMPLATE,
    apply_cap,
    cap_for,
    is_valid_profile,
)
from kernel.api.serialize import serialize_for_profile


# ----- Profile table -----


def test_raw_profile_has_no_caps():
    """`raw` profile must not truncate any hint. Default profile keeps
    historical API behavior (no caps) for callers that don't opt in."""
    for hint in ("short", "medium", "long", "rich", None):
        assert PROFILE_CAPS["raw"][hint] is None


def test_llm_profile_has_caps_per_hint():
    """`llm` profile must cap every hint. Concrete numbers chosen to
    accommodate modern context windows; if tuning is needed, update here."""
    assert PROFILE_CAPS["llm"]["short"] == 5_000
    assert PROFILE_CAPS["llm"]["medium"] == 50_000
    assert PROFILE_CAPS["llm"]["long"] == 500_000
    assert PROFILE_CAPS["llm"]["rich"] == 1_000_000
    # Default cap for unset hint is `medium` — matches the user decision
    # in the Session 27 plan.
    assert PROFILE_CAPS["llm"][None] == 50_000


def test_is_valid_profile():
    assert is_valid_profile("raw")
    assert is_valid_profile("llm")
    assert not is_valid_profile("preview")
    assert not is_valid_profile("")


# ----- cap_for -----


def test_cap_for_known_profile_and_hint():
    assert cap_for("short", "llm") == 5_000
    assert cap_for("rich", "llm") == 1_000_000


def test_cap_for_unset_hint_uses_profile_default():
    """Unset hint under `llm` defaults to `medium` (50K) via the None entry."""
    assert cap_for(None, "llm") == 50_000


def test_cap_for_raw_always_uncapped():
    for hint in ("short", "medium", "long", "rich", None):
        assert cap_for(hint, "raw") is None


def test_cap_for_unknown_profile_returns_none():
    """Unknown profile falls through to no cap (defensive)."""
    assert cap_for("rich", "unknown_profile") is None


# ----- apply_cap -----


def test_apply_cap_none_returns_value_unchanged():
    s = "x" * 100_000
    assert apply_cap(s, None) == s


def test_apply_cap_under_cap_returns_value_unchanged():
    s = "x" * 100
    assert apply_cap(s, 1_000) == s


def test_apply_cap_truncates_and_appends_marker():
    s = "x" * 10_000
    out = apply_cap(s, 5_000)
    assert len(out) <= 5_000, (
        "Output must fit within the cap (marker length is subtracted before "
        "slicing — see context_profile.apply_cap)"
    )
    assert "[… truncated — 10000 chars total." in out
    # Marker points to the escape hatch
    assert "?context_profile=raw" in out


def test_truncation_marker_points_to_raw_profile():
    """Pin: the marker text must reference `?context_profile=raw` as the
    escape hatch. If anyone renames the profile, this test fails first."""
    assert "?context_profile=raw" in TRUNCATION_MARKER_TEMPLATE


# ----- serialize_for_profile -----


class _FakeFieldDef:
    """Minimal stand-in for FieldDefinition during unit tests; only the
    `content_size_hint` attribute is read by `serialize_for_profile`."""

    def __init__(self, hint):
        self.content_size_hint = hint


class _FakeEntity:
    """Pydantic-shaped stand-in returning a fixed dict from model_dump."""

    def __init__(self, data):
        self._data = data

    def model_dump(self, by_alias=True):
        return dict(self._data)


def _make_cls(field_definitions):
    """Build a class with `_field_definitions` attribute mirroring how
    `kernel/entity/factory.py` attaches it to DynamicEntity."""

    class _Cls:
        pass

    _Cls._field_definitions = field_definitions
    return _Cls


def test_serialize_for_profile_raw_is_passthrough():
    """`raw` profile (default) must leave every field untouched even when
    fields have hints — keeps existing API consumers unaffected."""
    cls = _make_cls(
        {
            "body": _FakeFieldDef("rich"),
            "name": _FakeFieldDef("short"),
        }
    )
    entity = _FakeEntity({"body": "x" * 100_000, "name": "Alice"})
    out = serialize_for_profile(cls, entity, "raw")
    assert out["body"] == "x" * 100_000
    assert out["name"] == "Alice"


def test_serialize_for_profile_llm_caps_short_fields():
    """Field with hint=short under `llm` profile caps at 5K (minus marker)."""
    cls = _make_cls({"name": _FakeFieldDef("short")})
    entity = _FakeEntity({"name": "x" * 10_000})
    out = serialize_for_profile(cls, entity, "llm")
    assert len(out["name"]) <= 5_000
    assert "[… truncated" in out["name"]


def test_serialize_for_profile_llm_default_for_unset_hint():
    """Field with no hint (FieldDefinition.content_size_hint is None) gets
    the profile's default — 50K medium cap under `llm`."""
    cls = _make_cls({"summary": _FakeFieldDef(None)})
    entity = _FakeEntity({"summary": "x" * 100_000})
    out = serialize_for_profile(cls, entity, "llm")
    assert len(out["summary"]) <= 50_000
    assert "[… truncated" in out["summary"]


def test_serialize_for_profile_llm_leaves_short_value_untouched():
    """Truncation only fires when value exceeds the cap. A short body
    that fits gets returned as-is even under `llm`."""
    cls = _make_cls({"body": _FakeFieldDef("rich")})
    entity = _FakeEntity({"body": "small body"})
    out = serialize_for_profile(cls, entity, "llm")
    assert out["body"] == "small body"


def test_serialize_for_profile_ignores_non_string_fields():
    """Numeric, boolean, list, dict fields must never get truncated even
    if they have hints (defensive — hints only make sense for strings)."""
    cls = _make_cls(
        {
            "count": _FakeFieldDef("short"),
            "tags": _FakeFieldDef("short"),
            "meta": _FakeFieldDef("short"),
        }
    )
    entity = _FakeEntity({"count": 42, "tags": [1, 2, 3], "meta": {"k": "v"}})
    out = serialize_for_profile(cls, entity, "llm")
    assert out["count"] == 42
    assert out["tags"] == [1, 2, 3]
    assert out["meta"] == {"k": "v"}


def test_serialize_for_profile_kernel_entity_no_cap():
    """Kernel entities (no `_field_definitions`) short-circuit to to_dict.
    This is the architectural commitment: `policy lives on the entity
    definition`. Kernel entities have no FieldDefinition rows so they get
    no caps — Trace.outputs (potentially 1MB+ JSON-encoded string) flows
    through untouched. See context_profile.py module docstring."""
    cls = _make_cls(None)  # mirror "no field definitions" state
    entity = _FakeEntity({"outputs": "x" * 2_000_000})
    out = serialize_for_profile(cls, entity, "llm")
    assert len(out["outputs"]) == 2_000_000


def test_serialize_for_profile_kernel_entity_empty_dict_no_cap():
    """Empty `_field_definitions` dict (alternate kernel-entity shape) also
    short-circuits."""
    cls = _make_cls({})
    entity = _FakeEntity({"outputs": "x" * 2_000_000})
    out = serialize_for_profile(cls, entity, "llm")
    assert len(out["outputs"]) == 2_000_000


# ----- Source-level pins (mirrors test_meta_collection_field.py pattern) -----


def test_serialize_module_imports_context_profile_helpers():
    """Pin: serialize.py imports `apply_cap` and `cap_for` from
    context_profile. Without these the wrapper can't apply policy."""
    src = inspect.getsource(serialize_module)
    assert "from kernel.api.context_profile import apply_cap, cap_for" in src


def test_serialize_for_profile_uses_field_definitions():
    """Pin: `serialize_for_profile` reads `_field_definitions` off the
    entity class. If someone refactors the attribute name on
    DynamicEntity, this test catches it."""
    src = inspect.getsource(serialize_for_profile)
    assert "_field_definitions" in src
    # Short-circuit branch for kernel entities
    assert "if not field_definitions" in src


def test_context_profile_module_exports_required_symbols():
    """Pin: module exports the names callers expect. Renames break the
    import in serialize.py and registration.py."""
    for name in ("PROFILE_CAPS", "TRUNCATION_MARKER_TEMPLATE", "apply_cap", "cap_for", "is_valid_profile"):
        assert hasattr(context_profile_module, name), f"Missing export: {name}"
