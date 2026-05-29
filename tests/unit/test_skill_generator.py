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

from kernel.skill.generator import generate_entity_skill

# --- Helpers ---


def _field(
    type: str = "str",
    required: bool = False,
    enum_values: list | None = None,
    is_relationship: bool = False,
    relationship_target: str | None = None,
    is_state_field: bool = False,
    **extra,
):
    return SimpleNamespace(
        type=type,
        required=required,
        enum_values=enum_values,
        is_relationship=is_relationship,
        relationship_target=relationship_target,
        is_state_field=is_state_field,
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


# --- NEW: --data JSON-shape examples for create/update (Apr 27 trace) ---


def _extract_create_data_payload(out: str) -> str:
    """Pull the JSON between the single-quotes of `create --data '...'`.

    The skill emits `... create --data '{"k":"v"}' ...` on a markdown table
    line. We yank back the content between the first `--data '` and the
    closing `'` so tests can assert against actual JSON shape.
    """
    marker = "create --data '"
    idx = out.find(marker)
    assert idx >= 0, f"No `create --data` line in skill output:\n{out}"
    start = idx + len(marker)
    end = out.find("'", start)
    assert end > start, f"Unterminated --data quote in skill output:\n{out}"
    return out[start:end]


def _extract_update_data_payload(out: str) -> str:
    """Pull the JSON between the single-quotes of `update <id> --data '...'`."""
    marker = "update <id> --data '"
    idx = out.find(marker)
    assert idx >= 0, f"No `update <id> --data` line in skill output:\n{out}"
    start = idx + len(marker)
    end = out.find("'", start)
    return out[start:end]


def test_create_example_is_valid_json():
    """The example payload between --data quotes parses as JSON. Without
    this, an associate copy-pasting the example just gets a CLI error."""
    import json

    fields = {
        "title": _field(type="str", required=True),
        "company": _field(
            type="objectid", required=True, is_relationship=True, relationship_target="Company"
        ),
    }
    out = generate_entity_skill("Email", _definition(fields=fields))
    payload = _extract_create_data_payload(out)
    parsed = json.loads(payload)
    assert isinstance(parsed, dict)


def test_create_example_includes_required_fields():
    """Every required field appears as a key in the create example."""
    import json

    fields = {
        "title": _field(type="str", required=True),
        "company": _field(
            type="objectid", required=True, is_relationship=True, relationship_target="Company"
        ),
        "internal_notes": _field(type="str", required=False),  # optional — not required
    }
    out = generate_entity_skill("Email", _definition(fields=fields))
    parsed = json.loads(_extract_create_data_payload(out))
    assert "title" in parsed
    assert "company" in parsed


def test_create_example_excludes_optional_fields():
    """Optional fields are not in the example payload — keeps it minimal so
    associates know exactly what's required to instantiate."""
    import json

    fields = {
        "title": _field(type="str", required=True),
        "summary": _field(type="str", required=False),
        "tags": _field(type="list", required=False),
    }
    out = generate_entity_skill("Email", _definition(fields=fields))
    parsed = json.loads(_extract_create_data_payload(out))
    assert "title" in parsed
    assert "summary" not in parsed
    assert "tags" not in parsed


def test_create_example_uses_objectid_hex_for_relationship_fields():
    """Bug #9 root: agents pass dicts for relationship fields. Example
    must show a 24-char hex string so they copy that, not `{"name": ...}`.
    """
    import json
    import re

    fields = {
        "company": _field(
            type="objectid", required=True, is_relationship=True, relationship_target="Company"
        ),
    }
    out = generate_entity_skill("Email", _definition(fields=fields))
    parsed = json.loads(_extract_create_data_payload(out))
    company_value = parsed["company"]
    # 24-char lowercase hex.
    assert isinstance(company_value, str)
    assert re.fullmatch(r"[0-9a-f]{24}", company_value), (
        f"company placeholder should be 24-char hex string, got: {company_value!r}"
    )


def test_create_example_uses_first_enum_value():
    """For enum fields (e.g. priority: low|medium|high), the example uses
    the first allowed value — guaranteed-valid by definition."""
    import json

    fields = {
        "priority": _field(type="str", required=True, enum_values=["low", "medium", "high"]),
    }
    out = generate_entity_skill("Email", _definition(fields=fields))
    parsed = json.loads(_extract_create_data_payload(out))
    assert parsed["priority"] == "low"


def test_create_example_excludes_state_field():
    """The state field is set by the kernel default on create and is rejected
    on update — including it in the create example would mislead. Bug-prone
    because state field is required (e.g. `status: required=true,
    is_state_field=true`) by convention but should not be in the create
    payload."""
    import json

    fields = {
        "title": _field(type="str", required=True),
        "status": _field(
            type="str",
            required=True,
            is_state_field=True,
            enum_values=["received", "classified"],
        ),
    }
    out = generate_entity_skill("Email", _definition(fields=fields))
    parsed = json.loads(_extract_create_data_payload(out))
    assert "title" in parsed
    assert "status" not in parsed, "State field must not appear in create example"


def test_create_example_uses_iso_datetime_placeholder():
    """datetime fields get a real ISO 8601 placeholder, not a free-form
    string the agent has to invent. Pydantic accepts ISO 8601 directly."""
    import json
    import re

    fields = {
        "date": _field(type="datetime", required=True),
    }
    out = generate_entity_skill("Meeting", _definition(fields=fields))
    parsed = json.loads(_extract_create_data_payload(out))
    # Must be ISO 8601 with timezone (Z or +00:00).
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", parsed["date"])


def test_create_example_uses_typed_placeholders():
    """Each scalar type gets a JSON-typed placeholder (not a string for everything)."""
    import json

    fields = {
        "n": _field(type="int", required=True),
        "ratio": _field(type="float", required=True),
        "active": _field(type="bool", required=True),
        "items": _field(type="list", required=True),
    }
    out = generate_entity_skill("Sample", _definition(fields=fields))
    parsed = json.loads(_extract_create_data_payload(out))
    assert isinstance(parsed["n"], int)
    assert isinstance(parsed["ratio"], float)
    assert isinstance(parsed["active"], bool)
    assert isinstance(parsed["items"], list)


def test_update_example_is_valid_json_and_excludes_state_field():
    """Update example is also a working JSON payload, and never includes
    the state field — kernel rejects state changes via update endpoint."""
    import json

    fields = {
        "title": _field(type="str", required=True),
        "summary": _field(type="str", required=False),
        "status": _field(
            type="str",
            required=True,
            is_state_field=True,
            enum_values=["received", "classified"],
        ),
    }
    out = generate_entity_skill("Email", _definition(fields=fields))
    payload = _extract_update_data_payload(out)
    parsed = json.loads(payload)
    assert isinstance(parsed, dict)
    assert "status" not in parsed


def test_create_example_falls_back_to_braces_when_no_required_fields():
    """An entity with zero required fields falls back to `{...}` rather
    than emitting `--data ''` (which is invalid)."""
    fields = {
        "summary": _field(type="str", required=False),
    }
    out = generate_entity_skill("Note", _definition(fields=fields))
    # When no required fields, we still want a placeholder so the line is
    # readable. `{...}` is the canonical placeholder.
    assert "create --data '{...}'" in out


# --- NEW: list --data filter recipes (Apr 27 list-endpoint filter work) ---


def test_teaches_list_data_filter_with_relationship_field_example():
    """When an entity has a relationship field, the `list --data` example
    uses that field name so agents see how to filter by relationship —
    the most common case."""
    fields = {
        "company": _field(
            type="objectid", is_relationship=True, relationship_target="Company"
        ),
        "subject": _field(type="str"),
    }
    out = generate_entity_skill("Email", _definition(fields=fields))
    # Should emit a list example using the relationship field.
    assert "list --data '{\"company\":" in out


def test_teaches_list_data_filter_generic_when_no_relationship():
    """Entities without relationship fields get a generic <field>/<value> example."""
    fields = {"subject": _field(type="str"), "body": _field(type="str")}
    out = generate_entity_skill("Note", _definition(fields=fields))
    # Generic placeholder used.
    assert "list --data '{\"<field>\":\"<value>\"}'" in out


def test_does_not_claim_list_filter_unsupported():
    """Old skill output said arbitrary filter was 'not yet supported' —
    after the list-filter feature lands, the disclaimer is updated to
    'equality match only' (rather than 'unsupported')."""
    fields = {"subject": _field(type="str")}
    out = generate_entity_skill("Email", _definition(fields=fields))
    lower = out.lower()
    # The disclaimer about *operators* not being supported is fine — that's
    # truthful. The previous claim that *all arbitrary filters* aren't
    # supported is no longer accurate after the parser landed.
    assert "filtering by arbitrary fields on `list` is not yet supported" not in lower


# --- entity_resolve section (Apr 27 entity-resolve work) ---


def _resolve_capability(strategies: list):
    """Build an entity_resolve activation stand-in matching the
    CapabilityActivation Pydantic shape (capability + config)."""
    return SimpleNamespace(
        capability="entity_resolve",
        config={"strategies": strategies},
    )


def test_resolve_section_emitted_when_capability_activated():
    """When entity_resolve is in activated_capabilities, the skill output
    has a Resolve section spelling out the contract."""
    out = generate_entity_skill(
        "Company",
        _definition(
            activated_capabilities=[
                _resolve_capability(
                    [
                        {"type": "field_equality", "field": "domain", "normalizer": "domain"},
                        {"type": "fuzzy_string", "field": "name", "threshold": 0.85},
                    ]
                )
            ]
        ),
    )
    assert "### Resolve" in out
    # Configured fields appear so callers know what identity signals to send.
    assert "domain" in out
    assert "name" in out


def test_resolve_section_includes_working_cli_example():
    """The Resolve section emits a copy-pasteable CLI invocation using
    the configured fields. Agents see the exact shape of `--data`."""
    out = generate_entity_skill(
        "Company",
        _definition(
            activated_capabilities=[
                _resolve_capability(
                    [{"type": "field_equality", "field": "domain"}]
                )
            ]
        ),
    )
    # The candidate object uses the configured field name as a placeholder key.
    # The CLI command name is `entity-resolve` (kernel auto-derives from
    # cap_name `entity_resolve` via underscore-to-dash).
    assert "indemn company entity-resolve --data" in out
    assert '"candidate"' in out
    assert '"domain"' in out


def test_resolve_section_states_contract_explicitly():
    """The 'never auto-picks' contract is the load-bearing thing to teach.
    Without it agents will misuse the capability — picking the top-scored
    candidate silently when it's a fuzzy match."""
    out = generate_entity_skill(
        "Company",
        _definition(
            activated_capabilities=[
                _resolve_capability([{"type": "field_equality", "field": "domain"}])
            ]
        ),
    )
    lower = out.lower()
    assert "never auto-picks" in lower
    # Mentions both fuzzy (probabilistic) and tied-at-1.0 (ambiguous) cases
    # so callers know how to handle each.
    assert "fuzzy" in lower or "probabilistic" in lower
    assert "ambiguous" in lower or "tied" in lower


def test_no_resolve_section_when_capability_not_activated():
    """Entities without entity_resolve activated don't get a Resolve section
    (avoids confusing readers about a capability that's not on for them)."""
    out = generate_entity_skill(
        "Plain",
        _definition(activated_capabilities=[]),
    )
    assert "### Resolve" not in out


def test_resolve_section_appears_alongside_other_capabilities():
    """If both auto_classify and entity_resolve are activated, the Resolve
    section appears after the regular capabilities table."""
    out = generate_entity_skill(
        "Email",
        _definition(
            activated_capabilities=[
                _capability("auto_classify"),
                _resolve_capability([{"type": "fuzzy_string", "field": "subject"}]),
            ]
        ),
    )
    # Both auto-classify and resolve are present.
    assert "auto-classify" in out
    assert "### Resolve" in out
    # The Resolve subsection comes after the Capabilities table header.
    assert out.index("## Capabilities") < out.index("### Resolve")
