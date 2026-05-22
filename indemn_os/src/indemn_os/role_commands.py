"""Role management CLI — convenience create, add-watch, list with watches.

Replaces dynamic CRUD for Role with ergonomic named-flag commands.
"""

import typer

from indemn_os.client import CLIClient, render

role_app = typer.Typer(name="role", help="Role management")


@role_app.command("create")
def create_role(
    name: str = typer.Argument(..., help="Role name"),
    permissions: str = typer.Option(
        "{}",
        "--permissions",
        help="JSON permissions dict",
    ),
    watches: str = typer.Option(
        "[]",
        "--watches",
        help="JSON watch definitions array",
    ),
):
    """Create a role with permissions and watches.

    Example: indemn role create team_member \\
      --permissions '{"read": ["*"], "write": ["ActionItem"]}' \\
      --watches '[{"entity_type": "ActionItem", "event": "fields_changed"}]'
    """
    import orjson

    client = CLIClient()
    data = {
        "name": name,
        "permissions": orjson.loads(permissions),
        "watches": orjson.loads(watches),
    }
    result = client.post("/api/roles/", json=data)
    typer.echo(f"Created role: {name}")
    render(result, "json")


@role_app.command("list")
def list_roles(
    show_watches: bool = typer.Option(
        False,
        "--show-watches",
        help="Include watch definitions",
    ),
    fmt: str = typer.Option("json", "--format"),
):
    """List roles. Use --show-watches for full wiring view."""
    client = CLIClient()
    result = client.get("/api/roles/")
    if show_watches:
        fmt = "json"
    render(result, fmt)


@role_app.command("get")
def get_role(
    role_id: str,
    fmt: str = typer.Option("json", "--format"),
    context_profile: str = typer.Option(
        None,
        "--context-profile",
        help=(
            "Apply per-field truncation policy. Kernel entities are uncapped "
            "by design under all profiles; flag is accepted for harness compatibility."
        ),
    ),
):
    """Get a role by ID."""
    client = CLIClient()
    params: dict = {}
    if context_profile:
        params["context_profile"] = context_profile
    result = client.get(f"/api/roles/{role_id}", params=params)
    render(result, fmt)


@role_app.command("update")
def update_role(role_id: str, data: str = typer.Option(..., "--data")):
    """Update role fields."""
    import orjson

    client = CLIClient()
    result = client.put(f"/api/roles/{role_id}", json=orjson.loads(data))
    render(result, "json")


@role_app.command("add-watch")
def add_watch(
    role_name: str,
    watch: str = typer.Option(
        ...,
        "--watch",
        help="JSON watch definition",
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
