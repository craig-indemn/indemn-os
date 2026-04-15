"""Role management CLI — add-watch, list with watches."""

import typer

from kernel.cli.client import CLIClient, render

role_app = typer.Typer(name="role", help="Role management")


@role_app.command("add-watch")
def add_watch(
    role_name: str,
    watch: str = typer.Option(
        ..., "--watch", help="JSON watch definition",
    ),
):
    """Append a watch to an existing role (by name)."""
    import orjson

    client = CLIClient()
    result = client.post(
        "/api/_platform/role/add-watch",
        json={
            "role_name": role_name,
            "watch": orjson.loads(watch),
        },
    )
    typer.echo(f"Added watch to role '{role_name}'")
    render(result, "json")


@role_app.command("list")
def list_roles(
    show_watches: bool = typer.Option(
        False, "--show-watches", help="Include watch definitions",
    ),
    fmt: str = typer.Option("table", "--format"),
):
    """List roles. Use --show-watches for full wiring view."""
    client = CLIClient()
    params = {}
    if show_watches:
        params["show_watches"] = "true"
    result = client.get("/api/roles", params=params)
    if show_watches:
        fmt = "json"  # Watches are complex; force JSON
    render(result, fmt)
