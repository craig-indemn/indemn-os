"""Platform management commands — init, seed, upgrade."""

import typer

from indemn_os.client import CLIClient, render

platform_app = typer.Typer(name="platform", help="Platform management")


@platform_app.command("init")
def platform_init(
    admin_email: str = typer.Option(..., "--admin-email"),
    admin_password: str = typer.Option(..., "--admin-password", prompt=True, hide_input=True),
):
    """Bootstrap the first organization. One-time operation."""
    client = CLIClient()
    result = client.post(
        "/api/_platform/init",
        json={"admin_email": admin_email, "admin_password": admin_password},
    )
    typer.echo("Platform initialized!")
    typer.echo(f"  Org ID: {result['org_id']}")
    typer.echo(f"  Admin ID: {result['admin_id']}")
    typer.echo(f"  Access Token: {result['access_token']}")


@platform_app.command("seed")
def platform_seed(seed_dir: str = typer.Option("seed", "--dir")):
    """Load seed data (entity definitions, skills, roles)."""
    client = CLIClient()
    result = client.post("/api/_platform/seed", json={"seed_dir": seed_dir})
    typer.echo(f"Seed data loaded: {result}")


@platform_app.command("health")
def platform_health():
    """Check platform health."""
    client = CLIClient()
    result = client.get("/health")
    render(result, "json")


@platform_app.command("upgrade")
def platform_upgrade(
    dry_run: bool = typer.Option(True, "--dry-run/--apply",
                                 help="Preview changes (default) or apply them"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Upgrade platform — migrate entity definitions to current kernel schema.

    --dry-run (default): preview what would change
    --apply: execute the migrations

    Per design: kernel capability upgrades declare configuration schema
    versions. Entity definitions store which version they use. This command
    computes and applies migrations.
    """
    client = CLIClient()
    result = client.post(
        "/api/_platform/upgrade",
        json={"dry_run": dry_run},
    )
    render(result)
