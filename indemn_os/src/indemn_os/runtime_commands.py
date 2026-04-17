"""Runtime management CLI commands.

Operational commands for Runtime lifecycle: register-instance, heartbeat.
Standard CRUD (list, get, create) handled by dynamic entity registration.
"""

import typer

from indemn_os.client import CLIClient, render

runtime_app = typer.Typer(name="runtime", help="Runtime management")


@runtime_app.command("register-instance")
def register_instance(
    runtime_id: str = typer.Option(..., "--runtime-id"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Register a harness instance for a Runtime. Called at harness startup."""
    client = CLIClient()
    result = client.post(
        f"/api/runtimes/{runtime_id}/register-instance",
        json={},
    )
    render(result)


@runtime_app.command("heartbeat")
def heartbeat(
    runtime_id: str = typer.Option(..., "--runtime-id"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Send heartbeat for a Runtime instance. Called periodically by harness."""
    client = CLIClient()
    result = client.post(
        f"/api/runtimes/{runtime_id}/heartbeat",
        json={},
    )
    render(result)


@runtime_app.command("get")
def get_runtime(
    runtime_id: str,
    json_output: bool = typer.Option(False, "--json"),
):
    """Get a Runtime by ID."""
    client = CLIClient()
    result = client.get(f"/api/runtimes/{runtime_id}")
    render(result)


@runtime_app.command("list")
def list_runtimes(
    status: str = typer.Option(None, "--status"),
    kind: str = typer.Option(None, "--kind"),
    json_output: bool = typer.Option(False, "--json"),
):
    """List Runtimes."""
    client = CLIClient()
    params = {}
    if status:
        params["status"] = status
    if kind:
        params["kind"] = kind
    result = client.get("/api/runtimes/", params=params)
    render(result)
