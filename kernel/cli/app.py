"""Indemn OS CLI — entry point.

Fetches entity metadata from API and registers dynamic commands.
Static commands (platform, entity, queue) are always available.
Dynamic entity commands (submission list, email get, etc.) load from API metadata.
"""

import typer

from kernel.cli.client import CLIClient, render

app = typer.Typer(name="indemn", help="Indemn OS CLI")


def main():
    """Entry point. Registers static commands, then dynamic entity commands from API."""
    # Register static commands (always available)
    from kernel.cli.bulk_monitor import bulk_app
    from kernel.cli.entity_commands import entity_app
    from kernel.cli.integration_commands import integration_app
    from kernel.cli.lookup_commands import lookup_app
    from kernel.cli.org_commands import org_app
    from kernel.cli.platform_commands import platform_app
    from kernel.cli.queue_commands import queue_app

    app.add_typer(platform_app, name="platform")
    app.add_typer(entity_app, name="entity")
    app.add_typer(org_app, name="org")
    app.add_typer(queue_app, name="queue")
    app.add_typer(lookup_app, name="lookup")
    app.add_typer(bulk_app, name="bulk")
    app.add_typer(integration_app, name="integration")

    # Fetch entity metadata and register dynamic commands
    try:
        client = CLIClient()
        meta = client.get("/api/_meta/entities")
        for entity_meta in meta:
            _register_entity_commands(app, entity_meta, client)
    except Exception:
        pass  # API unavailable — static commands still work

    app()


def _register_entity_commands(parent: typer.Typer, meta: dict, client: CLIClient):
    """Register CLI commands for one entity type. Mirrors API registration."""
    name = meta["name"]
    slug = name.lower()
    entity_app = typer.Typer(name=slug, help=f"{name} operations")

    @entity_app.command("list")
    def list_cmd(
        limit: int = 20,
        offset: int = 0,
        status: str = None,
        fmt: str = typer.Option("table", "--format"),
    ):
        """List entities with filters."""
        params = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        result = client.get(f"/api/{slug}s", params=params)
        render(result, fmt)

    @entity_app.command("get")
    def get_cmd(entity_id: str, fmt: str = typer.Option("json", "--format")):
        """Get entity by ID."""
        result = client.get(f"/api/{slug}s/{entity_id}")
        render(result, fmt)

    @entity_app.command("create")
    def create_cmd(data: str = typer.Option(..., "--data")):
        """Create entity. Data as JSON string."""
        import orjson

        result = client.post(f"/api/{slug}s", json=orjson.loads(data))
        render(result, "json")

    @entity_app.command("update")
    def update_cmd(entity_id: str, data: str = typer.Option(..., "--data")):
        """Update entity fields."""
        import orjson

        result = client.put(f"/api/{slug}s/{entity_id}", json=orjson.loads(data))
        render(result, "json")

    if meta.get("state_machine"):

        @entity_app.command("transition")
        def transition_cmd(entity_id: str, to: str = typer.Option(..., "--to"), reason: str = None):
            """Transition entity state."""
            result = client.post(
                f"/api/{slug}s/{entity_id}/transition",
                json={"to": to, "reason": reason},
            )
            render(result, "json")

    # Register capability commands
    for cap in meta.get("capabilities", []):
        cap_slug = cap["name"].replace("_", "-")

        @entity_app.command(cap_slug)
        def cap_cmd(
            entity_id: str,
            auto: bool = False,
            data: str = None,
            _cap=cap["name"],
            _slug=slug,
        ):
            """Invoke a capability on an entity."""
            import orjson

            params = {"auto": "true"} if auto else {}
            body = orjson.loads(data) if data else {}
            result = client.post(
                f"/api/{_slug}s/{entity_id}/{_cap.replace('_', '-')}",
                json=body,
                params=params,
            )
            render(result, "json")

    # Register bulk commands for this entity type
    from kernel.cli.bulk_commands import register_bulk_commands
    register_bulk_commands(name, entity_app)

    parent.add_typer(entity_app, name=slug)
