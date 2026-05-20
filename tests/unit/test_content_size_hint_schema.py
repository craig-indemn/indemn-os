"""Tests that pin the FieldDefinition schema + meta endpoint expose
`content_size_hint`.

Why: the architectural commitment is "policy lives on the entity
definition." A field's content nature is declared via
`content_size_hint`; the kernel response serializer + harness consume
this metadata via the meta endpoint.

These tests pin:
- `content_size_hint` exists on FieldDefinition with the expected Literal.
- Default is None (existing entities and fields unaffected by upgrade).
- Both meta endpoints (`/_meta/entities`, `/_meta/entities/{name}`) emit
  `content_size_hint` in the field metadata response.
- The skill generator surfaces the hint in the Fields table when set.
- The GET route generator threads `context_profile` through to
  `serialize_for_profile`.
"""

import inspect
import typing

import kernel.api.meta as meta_module
import kernel.api.registration as registration_module
import kernel.skill.generator as skill_generator_module
from kernel.entity.definition import FieldDefinition


def test_field_definition_has_content_size_hint_attribute():
    """Pin: FieldDefinition declares `content_size_hint` with the four
    allowed values + None default. Renaming the field, dropping a value,
    or changing the default breaks the per-field policy contract."""
    fd = FieldDefinition(type="str")
    assert hasattr(fd, "content_size_hint")
    assert fd.content_size_hint is None  # default

    # Each allowed literal accepted
    for hint in ("short", "medium", "long", "rich"):
        fd = FieldDefinition(type="str", content_size_hint=hint)
        assert fd.content_size_hint == hint


def test_field_definition_rejects_invalid_hint():
    """Pin: Pydantic validation rejects values outside the Literal set."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        FieldDefinition(type="str", content_size_hint="huge")


def test_field_definition_literal_values_extractable():
    """Pin: `content_size_hint`'s Literal values are extractable via
    `typing.get_args`. The meta endpoint's `_extract_enum_values` relies
    on this pattern. If someone changes the type annotation away from
    Literal, the meta endpoint loses the ability to surface the allowed
    values."""
    fields = FieldDefinition.model_fields
    assert "content_size_hint" in fields
    # `Optional[Literal[...]]` annotation has `Literal[...]` as an arg.
    annotation = fields["content_size_hint"].annotation
    found_literal = False
    for arg in typing.get_args(annotation):
        if hasattr(arg, "__args__"):
            literal_args = typing.get_args(arg)
            if literal_args:
                assert set(literal_args) >= {"short", "medium", "long", "rich"}
                found_literal = True
                break
    assert found_literal, "Expected Literal[...] in the content_size_hint type annotation"


def test_meta_list_endpoint_serializes_content_size_hint():
    """Pin: `_get_field_metadata` (kernel-entity path) emits
    `content_size_hint` in the response dict. Without this, the CLI / UI
    can't see hints set on entity definitions."""
    src = inspect.getsource(meta_module._get_field_metadata)
    assert '"content_size_hint"' in src


def test_meta_detail_endpoint_serializes_content_size_hint():
    """Pin: detail endpoint (domain-entity path) emits
    `content_size_hint` per field, reading from FieldDefinition."""
    src = inspect.getsource(meta_module.get_entity_detail_metadata)
    assert '"content_size_hint": fdef.content_size_hint' in src


def test_skill_generator_surfaces_content_size_hint():
    """Pin: when `fdef.content_size_hint` is set, the generated skill
    markdown includes it in the field's Details column so associates
    seeing the skill know rich-content fields may be capped under llm
    profile."""
    src = inspect.getsource(skill_generator_module.generate_entity_skill)
    assert "content_size_hint" in src
    # Specifically renders as a `Content size:` detail row
    assert '"Content size:' in src or "'Content size:" in src or "Content size:" in src


def test_get_route_passes_context_profile_to_serializer():
    """Pin: the auto-generated GET route reads `context_profile` query
    param, validates it via `is_valid_profile`, and passes it to
    `serialize_for_profile`. Without this the field caps are never
    applied even when callers request `?context_profile=llm`."""
    src = inspect.getsource(registration_module)
    # Validation gate
    assert "is_valid_profile(context_profile)" in src
    # Serializer wiring
    assert "serialize_for_profile(entity_cls, entity, context_profile)" in src
    # Passes through to related-entities walker
    assert "profile=context_profile" in src


def test_build_related_entities_signature_accepts_profile():
    """Pin: `_build_related_entities` signature has `profile` so the
    related-entity walker can propagate truncation through nested
    serialization. Without this, depth-2+ responses would be uncapped
    even when the top-level entity is."""
    from kernel.message.emit import _build_related_entities

    sig = inspect.signature(_build_related_entities)
    assert "profile" in sig.parameters


def test_build_related_entities_uses_serialize_for_profile():
    """Pin: each nested entity in the related-walker output goes through
    `serialize_for_profile` (not raw `to_dict`). Otherwise nested fields
    skip the cap pipeline."""
    from kernel.message import emit as emit_module

    src = inspect.getsource(emit_module._build_related_entities)
    # Three call sites (forward, polymorphic, reverse) all use the new
    # serializer
    assert src.count("serialize_for_profile(") >= 3


# Defer pytest import to keep the optional dependency pattern explicit
import pytest  # noqa: E402
