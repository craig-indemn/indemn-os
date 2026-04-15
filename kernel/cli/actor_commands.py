"""Actor management CLI — add-role, add-auth, and convenience create.

Beyond the auto-generated CRUD (indemn actor create --data '...'),
these commands provide ergonomic wrappers for common operations.
"""

import typer

from kernel.cli.client import CLIClient, render

actor_app = typer.Typer(name="actor", help="Actor management")


@actor_app.command("add-role")
def add_role(
    email: str,
    role: str = typer.Option(..., "--role", help="Role name to add"),
):
    """Add a role to an actor (resolved by email)."""
    client = CLIClient()
    result = client.post(
        "/api/_platform/actor/add-role",
        json={"email": email, "role_name": role},
    )
    typer.echo(f"Added role '{role}' to {email}")
    render(result, "json")


@actor_app.command("add-auth")
def add_auth(
    email: str,
    method: str = typer.Option(..., "--method", help="Auth method: password, sso, magic_link"),
):
    """Add an authentication method to an actor."""
    client = CLIClient()
    result = client.post(
        "/api/_platform/actor/add-auth",
        json={"email": email, "method": method},
    )
    typer.echo(f"Added auth method '{method}' to {email}")
    render(result, "json")
