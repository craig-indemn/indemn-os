"""Integration management CLI commands.

Convenience create with named flags, plus credential management,
connectivity testing, and adapter version upgrades.
"""

import typer

from kernel.cli.client import CLIClient, render

integration_app = typer.Typer(name="integration", help="Integration management")


@integration_app.command("create")
def create_integration(
    owner: str = typer.Option(..., "--owner", help="org or actor"),
    name: str = typer.Option(..., "--name"),
    system_type: str = typer.Option(..., "--system-type", help="email, payment, etc."),
    provider: str = typer.Option(..., "--provider", help="outlook, gmail, stripe, etc."),
    access_roles: str = typer.Option(
        None, "--access-roles",
        help="Comma-separated role names for org-level access",
    ),
    actor_email: str = typer.Option(
        None, "--actor",
        help="Actor email for actor-level integrations (resolved to ID)",
    ),
):
    """Create an integration with named flags.

    Example: indemn integration create --owner org --name "Outlook" \\
      --system-type email --provider outlook --access-roles "ops,admin"
    """
    client = CLIClient()
    data = {
        "name": name,
        "owner_type": owner,
        "system_type": system_type,
        "provider": provider,
    }

    # Resolve owner_id
    if owner == "org":
        # Use current org (the API injects org_id from auth context)
        # Set owner_id to org_id — resolved server-side
        data["owner_id"] = "current_org"
    elif owner == "actor" and actor_email:
        # Resolve email to actor ID
        try:
            actors_resp = client.get(
                "/api/actors", params={"limit": 100},
            )
            for a in actors_resp:
                if a.get("email") == actor_email:
                    data["owner_id"] = a.get("_id") or a.get("id")
                    break
            else:
                typer.echo(
                    f"Warning: actor '{actor_email}' not found", err=True,
                )
        except Exception:
            typer.echo(
                f"Warning: could not resolve actor '{actor_email}'",
                err=True,
            )

    # Parse access roles
    if access_roles:
        roles = [r.strip() for r in access_roles.split(",")]
        data["access"] = {"roles": roles}

    result = client.post("/api/integrations", json=data)
    typer.echo(f"Created integration: {name} ({provider})")
    render(result, "json")


@integration_app.command("list")
def list_integrations(fmt: str = typer.Option("json", "--format")):
    """List integrations."""
    client = CLIClient()
    result = client.get("/api/integrations")
    render(result, fmt)


@integration_app.command("get")
def get_integration(
    integration_id: str, fmt: str = typer.Option("json", "--format"),
):
    """Get an integration by ID."""
    client = CLIClient()
    result = client.get(f"/api/integrations/{integration_id}")
    render(result, fmt)


@integration_app.command("set-credentials")
def set_credentials(
    integration_id: str,
    from_file: str = typer.Option(
        ..., "--from-file", help="Path to JSON credentials file",
    ),
):
    """Store credentials in Secrets Manager for an integration."""
    import orjson

    # Strip @ prefix if present (spec convention)
    path = from_file.lstrip("@")
    with open(path, "rb") as f:
        credentials = orjson.loads(f.read())

    client = CLIClient()
    result = client.post(
        f"/api/integrations/{integration_id}/set-credentials",
        json={"credentials": credentials},
    )
    render(result, "json")
    typer.echo(f"Credentials stored for integration {integration_id}")


@integration_app.command("rotate-credentials")
def rotate_credentials(integration_id: str):
    """Rotate credentials (provider-specific)."""
    client = CLIClient()
    result = client.post(
        f"/api/integrations/{integration_id}/rotate-credentials",
    )
    render(result, "json")


@integration_app.command("test")
def test_integration(integration_id: str):
    """Test connectivity by calling a read-only adapter method."""
    client = CLIClient()
    result = client.post(f"/api/integrations/{integration_id}/test")
    render(result, "json")


@integration_app.command("upgrade")
def upgrade_integration(
    integration_id: str,
    to_version: str = typer.Option(..., "--to-version"),
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run"),
):
    """Upgrade adapter version (e.g., outlook v2 -> v3)."""
    client = CLIClient()
    result = client.post(
        f"/api/integrations/{integration_id}/upgrade",
        json={"to_version": to_version, "dry_run": dry_run},
    )
    render(result, "json")
