"""Entity management commands — create, modify, enable, migrate, cleanup."""

import typer

from indemn_os.client import CLIClient, render

entity_app = typer.Typer(name="entity", help="Entity definition management")


@entity_app.command("create")
def create_entity_def(
    name: str,
    fields: str = typer.Option(..., "--fields", help="JSON field definitions"),
    state_machine: str = typer.Option(None, "--state-machine", help="JSON state machine"),
    computed_fields: str = typer.Option(None, "--computed-fields", help="JSON computed fields"),
    collection_name: str = typer.Option(None, help="MongoDB collection name"),
    description: str = typer.Option(None),
):
    """Create a new domain entity definition."""
    import orjson

    data = {
        "name": name,
        "collection_name": collection_name or name.lower() + "s",
        "fields": orjson.loads(fields),
    }
    if state_machine:
        data["state_machine"] = orjson.loads(state_machine)
    if computed_fields:
        data["computed_fields"] = orjson.loads(computed_fields)
    if description:
        data["description"] = description

    client = CLIClient()
    result = client.post("/api/entitydefinitions", json=data)
    typer.echo(f"Created entity definition: {result.get('name', name)}")
    render(result, "json")


@entity_app.command("list")
def list_entity_defs(fmt: str = typer.Option("json", "--format")):
    """List all entity definitions for the current org."""
    client = CLIClient()
    result = client.get("/api/entitydefinitions")
    render(result, fmt)


@entity_app.command("modify")
def modify_entity_def(
    name: str,
    add_field: str = typer.Option(None, "--add-field", help='JSON: {"field_name": {...}}'),
    remove_field: str = typer.Option(None, "--remove-field"),
):
    """Modify an entity definition (add/remove fields)."""
    import orjson

    data = {}
    if add_field:
        data["add_fields"] = orjson.loads(add_field)
    if remove_field:
        data["remove_fields"] = [remove_field]

    if not data:
        typer.echo("Nothing to modify. Use --add-field or --remove-field.", err=True)
        raise typer.Exit(1)

    client = CLIClient()
    result = client.put(f"/api/entitydefinitions/{name}/modify", json=data)
    added = result.get("added", [])
    removed = result.get("removed", [])
    typer.echo(f"Modified {name}: added={added}, removed={removed}")
    typer.echo("  (requires API restart to pick up changes)")


@entity_app.command("enable")
def enable_capability(
    entity_name: str,
    capability: str,
    config: str = typer.Option("{}", "--config", help="JSON capability config"),
):
    """Enable a kernel capability on an entity type."""
    import orjson

    client = CLIClient()
    result = client.put(
        f"/api/entitydefinitions/{entity_name}/enable-capability",
        json={"capability": capability, "config": orjson.loads(config)},
    )
    typer.echo(f"{result.get('status', 'done').title()} {capability} on {entity_name}")


@entity_app.command("migrate")
def migrate_entity(
    name: str,
    rename: str = typer.Option(None, help="old_field new_field"),
    add_field: str = typer.Option(None, "--add-field", help="JSON field definition"),
    remove_field: str = typer.Option(None, "--remove-field"),
    cleanup: bool = False,
    batch_size: int = typer.Option(100, "--batch-size"),
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run"),
):
    """Run a schema migration on an entity type."""
    import orjson

    operations = []
    if rename:
        parts = rename.split()
        if len(parts) != 2:
            typer.echo("--rename requires exactly two values: old_field new_field", err=True)
            raise typer.Exit(1)
        operations.append({"type": "rename_field", "from": parts[0], "to": parts[1]})
    if add_field:
        parsed = orjson.loads(add_field)
        for field_name, field_def in parsed.items():
            operations.append({"type": "add_field", "name": field_name, "field_def": field_def})
    if remove_field:
        operations.append({"type": "remove_field", "name": remove_field, "cleanup": cleanup})

    if not operations:
        typer.echo("Nothing to migrate. Use --rename, --add-field, or --remove-field.", err=True)
        raise typer.Exit(1)

    typer.echo(f"{'DRY RUN: ' if dry_run else ''}Migrating {name}")
    for op in operations:
        if op["type"] == "rename_field":
            typer.echo(f"  Rename: {op['from']} -> {op['to']}")
        elif op["type"] == "add_field":
            typer.echo(f"  Add field: {op['name']}")
        elif op["type"] == "remove_field":
            typer.echo(f"  Remove field: {op['name']}{' (+ cleanup)' if op.get('cleanup') else ''}")

    client = CLIClient()
    result = client.post(
        f"/api/entitydefinitions/{name}/migrate",
        json={
            "operations": operations,
            "dry_run": dry_run,
            "batch_size": batch_size,
        },
    )
    render(result, "json")
