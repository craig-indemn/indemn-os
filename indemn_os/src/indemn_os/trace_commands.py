"""Trace commands — CRUD for Trace kernel entities + debugging.

Trace CRUD (list, get, create, transition) for the Trace kernel entity.
Debugging commands (entity, cascade) for unified timeline and cascade views.
"""

import typer

from indemn_os.client import CLIClient, render

trace_app = typer.Typer(name="trace", help="Trace entities and execution debugging")


@trace_app.command("list")
def list_traces(
    associate: str = typer.Option(None, "--associate", help="Filter by associate name"),
    entity_type: str = typer.Option(None, "--entity-type"),
    status: str = typer.Option(None, "--status", help="created or evaluated"),
    execution_status: str = typer.Option(None, "--execution-status", help="success, error, or cancelled"),
    correlation_id: str = typer.Option(None, "--correlation-id"),
    limit: int = typer.Option(20, "--limit"),
):
    """List Trace entities with optional filters."""
    client = CLIClient()
    params: dict = {"limit": limit}
    if associate:
        params["associate_name"] = associate
    if entity_type:
        params["entity_type"] = entity_type
    if status:
        params["status"] = status
    if execution_status:
        params["execution_status"] = execution_status
    if correlation_id:
        params["correlation_id"] = correlation_id
    result = client.get("/api/traces/", params=params)
    render(result)


@trace_app.command("get")
def get_trace(trace_id: str):
    """Get a Trace entity by ID."""
    client = CLIClient()
    result = client.get(f"/api/traces/{trace_id}")
    render(result)


@trace_app.command("create")
def create_trace(
    data: str = typer.Option(..., "--data", help="JSON trace data"),
):
    """Create a Trace entity from JSON data."""
    import orjson

    client = CLIClient()
    result = client.post("/api/traces/", json=orjson.loads(data))
    render(result)


@trace_app.command("transition")
def transition_trace(
    trace_id: str,
    to: str = typer.Option(..., "--to", help="Target state (evaluated)"),
):
    """Transition a Trace entity's status."""
    client = CLIClient()
    result = client.post(
        f"/api/traces/{trace_id}/transition",
        json={"to_state": to},
    )
    render(result)


@trace_app.command("entity")
def trace_entity(
    entity_type: str,
    entity_id: str,
    limit: int = typer.Option(50, "--limit"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Unified timeline for one entity — changes + messages + message log.

    Shows every mutation, every message dispatch, every completion for this entity.
    """
    client = CLIClient()
    result = client.get(
        f"/api/trace/entity/{entity_type}/{entity_id}",
        params={"limit": limit},
    )

    if json_output or True:  # Always JSON for now
        render(result)
        return

    # TODO: formatted table output for human readability


@trace_app.command("cascade")
def trace_cascade(
    correlation_id: str,
    limit: int = typer.Option(100, "--limit"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Full execution tree by correlation_id.

    Shows every event in a cascade from trigger to completion —
    across entities, actors, and services.
    """
    client = CLIClient()
    result = client.get(
        f"/api/trace/cascade/{correlation_id}",
        params={"limit": limit},
    )

    if json_output or True:  # Always JSON for now
        render(result)
        return

    # TODO: formatted tree output for human readability
