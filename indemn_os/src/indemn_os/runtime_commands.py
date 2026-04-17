"""Runtime management CLI commands.

Operational commands for Runtime lifecycle: register-instance, heartbeat.
Standard CRUD (list, get, create) handled by dynamic entity registration.
"""

import typer

from indemn_os.client import CLIClient, render

runtime_app = typer.Typer(name="runtime", help="Runtime management")


@runtime_app.command("create")
def create_runtime(
    name: str = typer.Option(..., "--name"),
    kind: str = typer.Option(..., "--kind", help="async_worker, realtime_chat, realtime_voice"),
    framework: str = typer.Option("deepagents", "--framework"),
    framework_version: str = typer.Option("0.1.0", "--framework-version"),
    deployment_image: str = typer.Option("", "--deployment-image"),
    deployment_platform: str = typer.Option("railway", "--deployment-platform"),
    transport: str = typer.Option(None, "--transport"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Create a Runtime with service token — per G1.2 provisioning flow.

    Creates the Runtime entity + service actor + token in one call.
    Prints the service token to stdout ONCE. Store it securely.
    """
    client = CLIClient()
    # Step 1: Create Runtime entity
    runtime_data = {
        "name": name,
        "kind": kind,
        "framework": framework,
        "framework_version": framework_version,
        "deployment_platform": deployment_platform,
    }
    if deployment_image:
        runtime_data["deployment_image"] = deployment_image
    if transport:
        runtime_data["transport"] = transport

    runtime = client.post("/api/runtimes/", json=runtime_data)
    runtime_id = runtime["_id"]

    # Step 2: Create service token for this Runtime
    token_result = client.post(
        "/api/_platform/service-token",
        json={"runtime_id": runtime_id},
    )

    render({
        "runtime_id": runtime_id,
        "task_queue": f"runtime-{runtime_id}",
        "service_token": token_result["service_token"],
        "actor_id": token_result["actor_id"],
        "name": name,
        "kind": kind,
        "note": "Store service_token securely. It will not be shown again.",
    })


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


@runtime_app.command("transition")
def transition_runtime(
    runtime_id: str,
    to: str = typer.Option(..., "--to", help="Target state: deploying, active, draining, stopped"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Transition a Runtime through its lifecycle."""
    client = CLIClient()
    result = client.post(
        f"/api/runtimes/{runtime_id}/transition",
        json={"to": to},
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
