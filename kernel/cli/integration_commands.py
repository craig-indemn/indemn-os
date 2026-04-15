"""Integration management CLI commands.

Beyond auto-generated CRUD, these provide credential management,
connectivity testing, and adapter version upgrades.
"""

import typer

from kernel.cli.client import CLIClient, render

integration_app = typer.Typer(name="integration", help="Integration management")


@integration_app.command("set-credentials")
def set_credentials(
    integration_id: str,
    from_file: str = typer.Option(..., "--from-file", help="Path to JSON credentials file"),
):
    """Store credentials in Secrets Manager for an integration."""
    import orjson

    with open(from_file, "rb") as f:
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
    result = client.post(f"/api/integrations/{integration_id}/rotate-credentials")
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
    """Upgrade adapter version (e.g., outlook v2 → v3). [G-30]"""
    client = CLIClient()
    result = client.post(
        f"/api/integrations/{integration_id}/upgrade",
        json={"to_version": to_version, "dry_run": dry_run},
    )
    render(result, "json")
