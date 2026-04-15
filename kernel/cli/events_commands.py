"""CLI commands for the events stream.

`indemn events stream` — filtered JSON lines on stdout.
Used by harnesses to receive scoped events via subprocess.
"""

import typer

from kernel.cli.client import CLIClient

events_app = typer.Typer(name="events", help="Event stream operations")


@events_app.command("stream")
def stream_events(
    actor: str = typer.Option(None, help="Filter by target actor ID"),
    interaction: str = typer.Option(None, help="Filter by interaction ID"),
    entity_type: str = typer.Option(None, "--entity-type", help="Filter by entity type"),
):
    """Stream matching events as JSON lines on stdout."""
    client = CLIClient()
    params = {}
    if actor:
        params["actor"] = actor
    if interaction:
        params["interaction"] = interaction
    if entity_type:
        params["entity_type"] = entity_type

    with client.stream("GET", "/api/_stream/events", params=params) as response:
        for line in response.iter_lines():
            typer.echo(line)
