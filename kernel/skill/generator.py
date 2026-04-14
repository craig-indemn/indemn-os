"""Auto-generate entity skill markdown from entity definition.

This is the self-evidence property for documentation: define an entity,
its skill (documentation) exists immediately.
"""

from kernel.entity.definition import EntityDefinition


def generate_entity_skill(entity_name: str, definition: EntityDefinition) -> str:
    """Generate markdown skill from entity definition."""
    lines = [f"# {entity_name}\n"]

    if definition.description:
        lines.append(f"{definition.description}\n")

    # Fields
    lines.append("## Fields\n")
    lines.append("| Field | Type | Required |")
    lines.append("|-------|------|----------|")
    for name, fdef in definition.fields.items():
        lines.append(f"| {name} | {fdef.type} | {'Yes' if fdef.required else 'No'} |")

    # State machine
    if definition.state_machine:
        lines.append("\n## Lifecycle\n")
        for state, transitions in definition.state_machine.items():
            lines.append(f"- **{state}** -> {', '.join(transitions)}")

    # CLI commands
    slug = entity_name.lower()
    lines.append(f"\n## Commands\n")
    lines.append("| Command | Description |")
    lines.append("|---------|-------------|")
    lines.append(f"| `indemn {slug} list` | List with filters |")
    lines.append(f"| `indemn {slug} get <id>` | Get by ID |")
    lines.append(f"| `indemn {slug} create --data '...'` | Create new |")
    lines.append(f"| `indemn {slug} update <id> --data '...'` | Update fields |")
    if definition.state_machine:
        lines.append(f"| `indemn {slug} transition <id> --to <state>` | Change state |")
    for cap in definition.activated_capabilities or []:
        cap_name = cap.capability.replace("_", "-")
        lines.append(f"| `indemn {slug} {cap_name} <id> --auto` | {cap.capability} |")

    return "\n".join(lines)
