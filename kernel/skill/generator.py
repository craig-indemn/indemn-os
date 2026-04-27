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
"""

from kernel.capability import COLLECTION_LEVEL_CAPABILITIES
from kernel.entity.definition import EntityDefinition


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
    lines.append("## Writing\n")
    lines.append("| Command | Description |")
    lines.append("|---------|-------------|")
    lines.append(
        f"| `indemn {slug} create --data '{{...}}'` | "
        "Create a new record. See **Fields** for required keys. |"
    )
    lines.append(
        f"| `indemn {slug} update <id> --data '{{...}}'` | "
        "Patch fields. State field is rejected — use `transition` instead. |"
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
