"""Trace commands — unified debugging across changes, messages, and OTEL.

Per vision § 14:
- `indemn trace entity` — unified timeline for one entity
- `indemn trace cascade` — full execution tree by correlation_id
"""

import typer

from indemn_os.client import CLIClient, render

trace_app = typer.Typer(name="trace", help="Debugging and execution tracing")


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
