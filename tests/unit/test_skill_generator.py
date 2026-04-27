"""Tests for kernel.skill.generator.generate_entity_skill.

Bug #4 (generated entity skill teaches actual filter syntax) and Bug #6
(auto-generated skill has wrong fetch-new syntax) from os-bugs-and-shakeout.

Apr 24 GR Little Intelligence Extractor trace: the agent improvised
`indemn company list --filter ...` which doesn't exist, then `indemn meeting
fetch-new <id> --auto` (also wrong — fetch_new is collection-level, no id).
The generated entity skill was misleading the agent. These tests pin the
fix.

Use SimpleNamespace fixtures to construct minimal definition shapes — avoids
the Beanie Document machinery that's not relevant to the rendering logic.
"""

from types import SimpleNamespace

import pytest

from kernel.skill.generator import generate_entity_skill


# --- Helpers ---


def _field(
    type: str = "str",
    required: bool = False,
    enum_values: list | None = None,
    is_relationship: bool = False,
    relationship_target: str | None = None,
    **extra,
):
    return SimpleNamespace(
        type=type,
        required=required,
        enum_values=enum_values,
        is_relationship=is_relationship,
        relationship_target=relationship_target,
        **extra,
    )


def _capability(name: str, config: dict | None = None):
    return SimpleNamespace(capability=name, config=config or {})


def _definition(
    description: str | None = None,
    fields: dict | None = None,
    state_machine: dict | None = None,
    activated_capabilities: list | None = None,
):
    return SimpleNamespace(
        description=description,
        fields=fields or {},
        state_machine=state_machine,
        activated_capabilities=activated_capabilities or [],
    )


# --- Existing behavior preserved ---


def test_renders_entity_name_as_h1():
    out = generate_entity_skill("Email", _definition())
    assert out.startswith("# Email")


def test_renders_description_when_present():
    out = generate_entity_skill("Email", _definition(description="Raw email messages"))
    assert "Raw email messages" in out


def test_renders_fields_table():
    fields = {
        "subject": _field(type="str", required=True),
        "company": _field(type="objectid", is_relationship=True, relationship_target="Company"),
    }
    out = generate_entity_skill("Email", _definition(fields=fields))
    assert "## Fields" in out
    assert "subject" in out
    assert "company" in out
    assert "→ Company" in out


def test_renders_lifecycle_when_state_machine_present():
    sm = {"received": ["classified"], "classified": ["processed"]}
    out = generate_entity_skill("Email", _definition(state_machine=sm))
    assert "## Lifecycle" in out
    assert "received" in out
    assert "classified" in out


def test_no_lifecycle_when_no_state_machine():
    """Entities like Contact / Carrier (no state machine) should not emit a Lifecycle section."""
    out = generate_entity_skill("Carrier", _definition(state_machine=None))
    assert "## Lifecycle" not in out


def test_renders_basic_crud_commands():
    out = generate_entity_skill("Email", _definition(state_machine={"a": []}))
    assert "indemn email list" in out
    assert "indemn email get" in out
    assert "indemn email create" in out
    assert "indemn email update" in out
    assert "indemn email transition" in out


def test_no_transition_command_when_no_state_machine():
    out = generate_entity_skill("Carrier", _definition(state_machine=None))
    assert "indemn carrier transition" not in out


# --- NEW: filter syntax taught (Bug #4) ---


def test_teaches_status_filter_when_state_machine_present():
    """Stateful entities should show the --status filter recipe."""
    out = generate_entity_skill("Email", _definition(state_machine={"received": ["classified"]}))
    assert "--status" in out


def test_teaches_search_filter():
    """Every entity with name/title-shaped fields can use --search."""
    out = generate_entity_skill("Email", _definition())
    assert "--search" in out


def test_teaches_limit_and_offset_pagination():
    out = generate_entity_skill("Email", _definition())
    assert "--limit" in out


def test_does_not_emit_misleading_filter_promise():
    """Bug #4 was that the generated skill said 'List with filters' without showing the filters.
    The fix removes that misleading claim and replaces it with concrete recipes.
    """
    out = generate_entity_skill("Email", _definition())
    # The aspirational-without-recipe text from the original generator.
    assert "List with filters" not in out


def test_teaches_arbitrary_filter_limitation():
    """Capability #2 (arbitrary field filtering on list) is not yet supported.
    Skill should warn the agent so it doesn't improvise --filter / --data on list.
    """
    out = generate_entity_skill("Email", _definition())
    # Some signal that filter-by-arbitrary-field isn't supported. Phrasing intentionally
    # loose; tests behavior, not exact wording.
    lower = out.lower()
    assert ("not yet supported" in lower) or ("not supported" in lower)


# --- NEW: relationship navigation taught ---


def test_teaches_include_related_for_forward_navigation():
    """`--depth N --include-related` is the way to load forward-related entities inline.
    Agents need this taught so they don't try reverse lookups via list filters.
    """
    fields = {
        "company": _field(type="objectid", is_relationship=True, relationship_target="Company"),
    }
    out = generate_entity_skill("Email", _definition(fields=fields))
    assert "--include-related" in out
    assert "--depth" in out


# --- NEW: ObjectId field warning (prevents Bug #9) ---


def test_warns_about_objectid_string_form_when_relationship_present():
    """Bug #9: associates pass dicts instead of ObjectId hex strings, dead-letter the message.
    Skill should make it unmissable that relationships take string hex IDs.
    """
    fields = {
        "company": _field(type="objectid", is_relationship=True, relationship_target="Company"),
    }
    out = generate_entity_skill("Email", _definition(fields=fields))
    lower = out.lower()
    assert "objectid" in lower
    # Hex / 24-char / not dict — at least one of these phrasings.
    has_warning = any(
        phrase in lower for phrase in ["hex string", "24-char", "not a dict", "not as a dict"]
    )
    assert has_warning, f"Expected ObjectId guidance in skill output, got: {out}"


def test_no_objectid_warning_when_no_relationships():
    """Entities without relationship fields don't need the ObjectId-as-hex warning."""
    fields = {"name": _field(type="str", required=True)}
    out = generate_entity_skill("Carrier", _definition(fields=fields))
    # If a "Carrier" has no objectid relationships, skip the warning to keep skill lean.
    # The presence of the literal word "objectid" is allowed (it's a type), but the
    # specific guidance about hex strings shouldn't be there.
    assert "hex string" not in out.lower()
    assert "24-char" not in out.lower()


# --- FIXED: capability rendering (Bug #6) ---


def test_collection_level_capability_no_id_no_auto():
    """Bug #6: fetch_new is collection-level — emits without <id> and without --auto.

    Before: `indemn meeting fetch-new <id> --auto`  (wrong — invalid CLI shape)
    After:  `indemn meeting fetch-new --data '{...}'`
    """
    out = generate_entity_skill(
        "Meeting", _definition(activated_capabilities=[_capability("fetch_new")])
    )
    # Find the fetch-new line.
    lines = [line for line in out.split("\n") if "fetch-new" in line]
    assert lines, f"No fetch-new line found in: {out}"
    fetch_line = " ".join(lines)
    assert "<id>" not in fetch_line, f"fetch-new should not include <id>: {fetch_line}"
    assert "--auto" not in fetch_line, f"fetch-new should not include --auto: {fetch_line}"


def test_instance_level_capability_keeps_id_and_auto():
    """auto_classify is instance-level — keeps <id> and --auto."""
    out = generate_entity_skill(
        "Email", _definition(activated_capabilities=[_capability("auto_classify")])
    )
    lines = [line for line in out.split("\n") if "auto-classify" in line]
    assert lines, f"No auto-classify line found in: {out}"
    classify_line = " ".join(lines)
    assert "<id>" in classify_line
    assert "--auto" in classify_line


def test_no_capability_section_when_none_activated():
    """Entities without activated capabilities don't render bogus capability lines."""
    out = generate_entity_skill("Email", _definition(activated_capabilities=[]))
    assert "auto-classify" not in out
    assert "fetch-new" not in out
