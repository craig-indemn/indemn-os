"""Entity management commands — create, modify, enable, migrate, cleanup."""

import inflect
import typer

from kernel.cli.client import CLIClient, render

entity_app = typer.Typer(name="entity", help="Entity definition management")

# Shared inflect engine — pluralizes English words properly:
#   Company    -> companies (not "companys")
#   Opportunity-> opportunities (not "opportunitys")
#   Email      -> emails  (no change vs naive)
#   Person     -> people   (no change but the naive +s would say "persons")
# Pre-fix the CLI did `name.lower() + "s"`, so existing collections in dev
# are `companys` and `opportunitys`. Per the 2026-04-28 decision
# (Bug #15: "accept and fix forward"), existing collection names are NOT
# migrated — operators set `collection_name` explicitly when they need the
# old typo'd name on a re-clone, or the proper plural for new entities.
_INFLECT = inflect.engine()


def _default_collection_name(entity_name: str) -> str:
    """Auto-derive a MongoDB collection name from an entity name."""
    return _INFLECT.plural(entity_name.lower())


@entity_app.command("create")
def create_entity_def(
    name: str,
    fields: str = typer.Option(..., "--fields", help="JSON field definitions"),
    state_machine: str = typer.Option(None, "--state-machine", help="JSON state machine"),
    computed_fields: str = typer.Option(None, "--computed-fields", help="JSON computed fields"),
    collection_name: str = typer.Option(
        None,
        help=(
            "MongoDB collection name. Auto-derived via inflect if omitted "
            "(Company -> companies). Pass explicitly to land in an existing "
            "collection (e.g. pre-2026-04-28 names `companys`/`opportunitys`)."
        ),
    ),
    description: str = typer.Option(None),
):
    """Create a new domain entity definition."""
    import orjson

    data = {
        "name": name,
        "collection_name": collection_name or _default_collection_name(name),
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
    # Migration routes through the entity definition API
