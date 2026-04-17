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
