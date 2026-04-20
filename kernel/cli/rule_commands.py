"""Rule management CLI — create, list, update, archive rules."""

import typer

from kernel.cli.client import CLIClient, render

rule_app = typer.Typer(name="rule", help="Rule management")


@rule_app.command("list")
def list_rules(
    entity: str = typer.Option(None, "--entity", help="Filter by entity type"),
    capability: str = typer.Option(None, "--capability"),
    status: str = typer.Option("active", "--status"),
    fmt: str = typer.Option("json", "--format"),
):
    """List rules for the current org."""
    client = CLIClient()
    params = {}
    if entity:
        params["entity_type"] = entity
    if capability:
        params["capability"] = capability
    if status:
        params["status"] = status
    result = client.get("/api/rules/", params=params)
    render(result, fmt)


@rule_app.command("create")
def create_rule(
    entity: str = typer.Option(..., "--entity", help="Entity type (e.g., Email)"),
    capability: str = typer.Option(..., "--capability", help="Capability name"),
    name: str = typer.Option(None, "--name", help="Human-readable rule name"),
    when: str = typer.Option(..., "--when", help="JSON conditions"),
    action: str = typer.Option(..., "--action", help="set_fields or force_reasoning"),
    sets: str = typer.Option(None, "--sets", help="JSON field values for set_fields action"),
    forces_reasoning_reason: str = typer.Option(
        None, "--forces-reasoning-reason", help="Reason for force_reasoning action"
    ),
    priority: int = typer.Option(100, "--priority"),
    status: str = typer.Option("active", "--status"),
):
    """Create a new rule."""
    import orjson

    data = {
        "entity_type": entity,
        "capability": capability,
        "conditions": orjson.loads(when),
        "action": action,
        "priority": priority,
        "status": status,
    }
    if name:
        data["name"] = name
    if sets:
        data["sets"] = orjson.loads(sets)
    if forces_reasoning_reason:
        data["forces_reasoning_reason"] = forces_reasoning_reason

    client = CLIClient()
    result = client.post("/api/rules/", json=data)
    typer.echo(f"Created rule: {name or result.get('_id', '?')}")
    render(result, "json")


@rule_app.command("archive")
def archive_rule(rule_id: str):
    """Archive a rule (soft delete)."""
    client = CLIClient()
    result = client.delete(f"/api/rules/{rule_id}")
    typer.echo(f"Archived rule: {rule_id}")
    render(result, "json")
