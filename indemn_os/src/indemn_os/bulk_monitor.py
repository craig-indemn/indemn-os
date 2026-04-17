"""Bulk operation monitoring CLI commands.

Provides status, list, and cancel for bulk operations
backed by the /api/bulk endpoints.
"""

import typer

from indemn_os.client import CLIClient, render

bulk_app = typer.Typer(name="bulk", help="Bulk operation monitoring")


@bulk_app.command("status")
def bulk_status(workflow_id: str):
    """Check status of a running bulk operation."""
    client = CLIClient()
    result = client.get(f"/api/bulk/{workflow_id}")
    render(result, "json")


@bulk_app.command("list")
def bulk_list(status: str = typer.Option(None, "--status")):
    """List active and recent bulk operations."""
    client = CLIClient()
    params = {}
    if status:
        params["status"] = status
    result = client.get("/api/bulk", params=params)
    render(result, "table")


@bulk_app.command("cancel")
def bulk_cancel(workflow_id: str):
    """Cancel a running bulk operation at the next batch boundary."""
    client = CLIClient()
    result = client.post(f"/api/bulk/{workflow_id}/cancel")
    render(result, "json")
    typer.echo(f"Cancel requested for {workflow_id}")
