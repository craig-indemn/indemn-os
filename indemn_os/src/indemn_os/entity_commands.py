"""Entity management commands — create, modify, enable, migrate, cleanup."""

import inflect
import typer

from indemn_os.client import CLIClient, render

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
    """Auto-derive a MongoDB collection name from an entity name.
    Operators should pass `--collection-name` explicitly when the entity
    needs to land in an existing collection (e.g. cross-org re-clone of a
    pre-2026-04-28 typo'd name like `companys`)."""
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
            "MongoDB collection name. Auto-derived via the `inflect` library "
            "if omitted (Company -> companies). Pass explicitly when you need "
            "to land in an existing collection — e.g. cross-org clones of a "
            "pre-2026-04-28 typo'd name like `companys`."
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


@entity_app.command("get")
def get_entity_def(name: str, fmt: str = typer.Option("json", "--format")):
    """Get an entity definition by name."""
    client = CLIClient()
    result = client.get(f"/api/entitydefinitions/{name}")
    render(result, fmt)


@entity_app.command("delete")
def delete_entity_def(
    name: str,
    force: bool = typer.Option(False, "--force", help="Skip confirmation"),
):
    """Delete an entity definition and its associated skill."""
    if not force:
        confirm = typer.confirm(f"Delete entity definition '{name}'? This cannot be undone")
        if not confirm:
            raise typer.Abort()

    client = CLIClient()
    result = client.delete(f"/api/entitydefinitions/{name}")
    typer.echo(f"Deleted entity definition: {name}")
    if result.get("skill_deleted"):
        typer.echo(f"  Also deleted skill: {name}")
    if result.get("collection_dropped"):
        typer.echo(f"  Also dropped collection: {result['collection_dropped']}")


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
