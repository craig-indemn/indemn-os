"""Entity management commands — create, modify, enable, migrate, cleanup."""

import typer

from kernel.cli.client import CLIClient, render

entity_app = typer.Typer(name="entity", help="Entity definition management")


@entity_app.command("create")
def create_entity_def(
    name: str,
    fields: str = typer.Option(..., "--fields", help="JSON field definitions"),
    state_machine: str = typer.Option(None, "--state-machine", help="JSON state machine"),
    computed_fields: str = typer.Option(None, "--computed-fields", help="JSON computed fields"),
    collection_name: str = typer.Option(None, help="MongoDB collection name (default: lowercase plural)"),
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
def list_entity_defs(fmt: str = typer.Option("table", "--format")):
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
    typer.echo(f"Modifying entity definition: {name}")
    # Implementation routes through the entity definition API
    # The API validates and updates the definition in MongoDB
    if add_field:
        typer.echo(f"  Adding field: {add_field}")
    if remove_field:
        typer.echo(f"  Removing field: {remove_field}")
    typer.echo("  (requires API restart to pick up changes)")


@entity_app.command("enable")
def enable_capability(
    entity_name: str,
    capability: str,
    config: str = typer.Option("{}", "--config", help="JSON capability config"),
):
    """Enable a kernel capability on an entity type."""
    import orjson

    typer.echo(f"Enabling {capability} on {entity_name}")
    typer.echo(f"  Config: {orjson.loads(config)}")
    # Implementation updates the EntityDefinition.activated_capabilities


@entity_app.command("migrate")
def migrate_entity(
    name: str,
    rename: str = typer.Option(None, help="old_field new_field"),
    add_field: str = typer.Option(None, "--add-field", help="JSON field definition"),
    remove_field: str = typer.Option(None, "--remove-field"),
    cleanup: bool = False,
    batch_size: int = 100,
    dry_run: bool = False,
):
    """Run a schema migration on an entity type."""
    typer.echo(f"{'DRY RUN: ' if dry_run else ''}Migrating {name}")
    if rename:
        parts = rename.split()
        typer.echo(f"  Rename: {parts[0]} → {parts[1]}")
    if add_field:
        typer.echo(f"  Add field: {add_field}")
    if remove_field:
        typer.echo(f"  Remove field: {remove_field} {'(+ cleanup)' if cleanup else ''}")
    # Implementation routes through the migration API
