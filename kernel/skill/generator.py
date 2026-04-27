"""Auto-generate entity skill markdown from entity definition.

This is the self-evidence property for documentation: define an entity,
its skill (documentation) exists immediately.

Bug #4 (os-bugs-and-shakeout): the generated skill emitted "List with filters"
without showing the filters, leading agents to improvise `--filter` (which
doesn't exist) and fail. Bug #6: collection-level capabilities (e.g.
fetch_new) were rendered with `<id>` and `--auto` — both invalid for that
shape. This module's `generate_entity_skill` teaches the actual CLI surface:
real filter recipes, forward-relationship navigation, ObjectId guidance for
relationship fields, and correct capability shapes per kind.

Apr 27 Alliance trace surfaced an extension: the auto-generated skill
documents a generic `--data '{...}'` for create/update but never shows what
{...} should look like. Associates and humans had to guess the right
payload. The `_example_*` helpers below render a working example payload
per command, with required fields populated from type-appropriate
placeholders (real-shaped 24-hex for ObjectId, first allowed value for
enums, ISO 8601 for datetimes, etc.).
"""

import json

from kernel.capability import COLLECTION_LEVEL_CAPABILITIES
from kernel.entity.definition import EntityDefinition, FieldDefinition

# Placeholder values per field type. Chosen to be syntactically valid in JSON
# AND visually obvious as placeholders (e.g. zeroes, a real-shaped ObjectId,
# a fixed ISO datetime). Associates can copy-paste the example, swap the
# placeholders for real values, and the payload validates.
_OBJECTID_PLACEHOLDER = "69eb95f22b0a508618923977"
_DATETIME_PLACEHOLDER = "2026-04-27T00:00:00Z"
_DATE_PLACEHOLDER = "2026-04-27"

_TYPE_PLACEHOLDERS: dict[str, object] = {
    "int": 0,
    "float": 0.0,
    "decimal": 0.0,
    "bool": False,
    "list": [],
    "dict": {},
    "datetime": _DATETIME_PLACEHOLDER,
    "date": _DATE_PLACEHOLDER,
    "objectid": _OBJECTID_PLACEHOLDER,
}


def _placeholder_for_field(field_name: str, fdef: FieldDefinition) -> object:
    """Return a JSON-serializable placeholder value for a field.

    Priority:
      1. enum_values present → first allowed value (most likely correct)
      2. type-mapped placeholder (objectid/int/datetime/...)
      3. fall back to a quoted angle-bracket hint using the field name

    The field name is woven into the str fallback (e.g. `<title>`) so the
    example self-documents what value the agent should provide.
    """
    if fdef.enum_values:
        return fdef.enum_values[0]
    if fdef.type in _TYPE_PLACEHOLDERS:
        return _TYPE_PLACEHOLDERS[fdef.type]
    # str (and any unknown type): hint with the field name.
    return f"<{field_name}>"


def _build_create_example(definition: EntityDefinition) -> dict:
    """Build the example payload for a create command.

    Includes every required field with a type-appropriate placeholder so the
    associate can copy the JSON and swap placeholders for real values.
    Excludes the state field — the kernel sets it to its default on create
    and rejects state changes on update.
    """
    example: dict = {}
    for name, fdef in definition.fields.items():
        if not fdef.required:
            continue
        if fdef.is_state_field:
            # State field is controlled by the state machine, not the create
            # payload. Including it would mislead.
            continue
        example[name] = _placeholder_for_field(name, fdef)
    return example


def _build_update_example(definition: EntityDefinition) -> dict:
    """Build the example payload for an update command.

    Picks one to three writable, non-state, non-required fields as a
    representative example. Update bodies are partial patches — showing
    every field would imply they all need to be sent on every update.
    """
    example: dict = {}
    for name, fdef in definition.fields.items():
        if fdef.is_state_field:
            continue
        # Skip auto-managed fields. Pydantic v2 stores these as fields too,
        # but they should not appear in user-edit examples.
        if name in ("_id", "id", "org_id", "version", "created_at", "updated_at", "created_by"):
            continue
        example[name] = _placeholder_for_field(name, fdef)
        if len(example) >= 3:
            break
    return example


def _format_json_inline(payload: dict) -> str:
    """Render a dict as compact single-line JSON suitable for `--data '...'`.

    Compact (no spaces around separators) keeps the example fitting in
    one CLI invocation copy-paste; agents commonly trip up when they have
    to assemble multi-line JSON inside shell quoting.
    """
    return json.dumps(payload, separators=(",", ":"))


def generate_entity_skill(entity_name: str, definition: EntityDefinition) -> str:
    """Generate markdown skill from entity definition."""
    lines = [f"# {entity_name}\n"]

    if definition.description:
        lines.append(f"{definition.description}\n")

    # --- Fields ---
    lines.append("## Fields\n")
    lines.append("| Field | Type | Required | Details |")
    lines.append("|-------|------|----------|---------|")
    has_relationship = False
    for name, fdef in definition.fields.items():
        details = []
        if fdef.enum_values:
            details.append(f"Values: {', '.join(fdef.enum_values)}")
        if fdef.is_relationship and fdef.relationship_target:
            details.append(f"→ {fdef.relationship_target}")
            has_relationship = True
        detail_str = "; ".join(details) if details else ""
        req = "Yes" if fdef.required else "No"
        lines.append(f"| {name} | {fdef.type} | {req} | {detail_str} |")

    # --- Lifecycle ---
    if definition.state_machine:
        lines.append("\n## Lifecycle\n")
        for state, transitions in definition.state_machine.items():
            lines.append(f"- **{state}** -> {', '.join(transitions)}")

    slug = entity_name.lower()

    # --- Read commands ---
    lines.append("\n## Reading\n")
    lines.append("| Command | Description |")
    lines.append("|---------|-------------|")
    lines.append(f"| `indemn {slug} list` | List records (most recent first) |")
    if definition.state_machine:
        lines.append(
            f"| `indemn {slug} list --status <state>` | "
            "Filter by current state (see Lifecycle above) |"
        )
    lines.append(
        f"| `indemn {slug} list --search <text>` | "
        "Substring match on `name` or `title` field |"
    )
    lines.append(
        f"| `indemn {slug} list --limit 50 --offset 100 --sort -created_at` | "
        "Pagination + sort (prefix `-` for descending) |"
    )
    lines.append(f"| `indemn {slug} get <id>` | Get a single record by ObjectId |")
    lines.append(
        f"| `indemn {slug} get <id> --depth 2 --include-related` | "
        "Get with forward-related entities resolved inline |"
    )

    lines.append(
        "\n*Filtering by arbitrary fields on `list` is not yet supported. "
        "For navigation between entities, prefer `--depth N --include-related` "
        "on `get`.*\n"
    )

    # --- Write commands ---
    create_example = _build_create_example(definition)
    update_example = _build_update_example(definition)

    lines.append("## Writing\n")
    lines.append("| Command | Description |")
    lines.append("|---------|-------------|")
    create_payload = _format_json_inline(create_example) if create_example else "{...}"
    lines.append(
        f"| `indemn {slug} create --data '{create_payload}'` | "
        "Create a new record (example shows required fields). |"
    )
    update_payload = _format_json_inline(update_example) if update_example else "{...}"
    lines.append(
        f"| `indemn {slug} update <id> --data '{update_payload}'` | "
        "Patch fields (any subset). State field is rejected — use `transition`. |"
    )
    if definition.state_machine:
        lines.append(
            f"| `indemn {slug} transition <id> --to <state> --reason '<why>'` | "
            "State machine transition (only valid targets accepted) |"
        )

    if has_relationship:
        lines.append(
            "\n*Relationship fields (`→ <Entity>` above) take ObjectId hex strings, "
            "**not** dicts. Pass `\"company\": \"69eb95f22b0a508618923977\"`, "
            "not `\"company\": {\"name\": \"Acme\"}`. Look up the related entity "
            "first via `indemn <other> list --search '...'`, then use its `_id`.*\n"
        )

    # --- Capabilities ---
    capabilities = definition.activated_capabilities or []
    if capabilities:
        lines.append("## Capabilities\n")
        lines.append("| Command | Description |")
        lines.append("|---------|-------------|")
        for cap in capabilities:
            cap_name = cap.capability
            cli_name = cap_name.replace("_", "-")
            if cap_name in COLLECTION_LEVEL_CAPABILITIES:
                # Collection-level: no entity_id, takes --data params
                lines.append(
                    f"| `indemn {slug} {cli_name} --data '{{...}}'` | "
                    f"{cap_name} (collection-level — creates/syncs records) |"
                )
            else:
                # Instance-level: <id> + --auto for rules-first / LLM fallback
                lines.append(
                    f"| `indemn {slug} {cli_name} <id> --auto` | "
                    f"{cap_name} (rules first, LLM fallback if `--auto`) |"
                )

    return "\n".join(lines)
