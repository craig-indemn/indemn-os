"""Queue management commands — stats, dead-letter, retry."""

import typer

from indemn_os.client import CLIClient, render

queue_app = typer.Typer(name="queue", help="Message queue management")


@queue_app.command("stats")
def queue_stats(role: str = None, fmt: str = typer.Option("json", "--format")):
    """Show queue statistics (pending, processing, dead-letter counts per role)."""
    client = CLIClient()
    params = {}
    if role:
        params["role"] = role
    result = client.get("/api/_meta/queue-stats", params=params)
    render(result, fmt)


@queue_app.command("dead-letter")
def dead_letter_list(limit: int = 20, fmt: str = typer.Option("json", "--format")):
    """List dead-letter messages."""
    client = CLIClient()
    result = client.get("/api/message_queues/", params={"status": "dead_letter", "limit": limit})
    render(result, fmt)


@queue_app.command("retry")
def retry_message(message_id: str):
    """Retry a dead-letter message by resetting to pending."""
    client = CLIClient()
    result = client.post(f"/api/message_queues/{message_id}/retry")
    typer.echo(f"Message {message_id} reset to pending")
    render(result, "json")


@queue_app.command("complete")
def complete_message(
    message_id: str,
    json_output: bool = typer.Option(False, "--json"),
):
    """Mark a message as completed. Standard queue verb used by any claimer."""
    client = CLIClient()
    result = client.post(f"/api/message_queues/{message_id}/complete")
    render(result)


@queue_app.command("fail")
def fail_message(
    message_id: str,
    reason: str = typer.Option("", "--reason"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Mark a message as failed. Standard queue verb used by any claimer."""
    client = CLIClient()
    result = client.post(
        f"/api/message_queues/{message_id}/fail",
        json={"reason": reason},
    )
    render(result)


@queue_app.command("drain")
def drain_parked(
    role: str = typer.Option(..., "--role", help="Role whose parked messages to drain"),
    limit: int = typer.Option(20, "--limit", help="Max messages to re-emit (max 500)"),
):
    """Re-emit parked messages as fresh pending messages for a role.

    Use after reactivating a suspended associate to replay historical
    backlog at a controlled pace. Each parked message gets a fresh ID;
    the original retires to dead_letter.

    Example: indemn queue drain --role email_classifier --limit 20
    """
    client = CLIClient()
    result = client.post("/api/queue/drain", json={"role": role, "limit": limit})
    reemitted = result.get("reemitted", 0)
    remaining = result.get("remaining_parked", 0)
    typer.echo(f"Drained {reemitted} parked messages for {role} ({remaining} remaining)")


@queue_app.command("extend-visibility")
def extend_visibility(
    message_id: str,
    json_output: bool = typer.Option(False, "--json"),
):
    """Extend the visibility timeout on a still-claimed message.

    Used by long-running activities (cron_runner subprocess, agent loops)
    to keep the Mongo queue's view of liveness in sync with the activity's
    actual progress. Bug #50 fix — paired with the Temporal activity
    heartbeat that Bug #49 added."""
    client = CLIClient()
    result = client.post(f"/api/message_queues/{message_id}/extend-visibility")
    render(result)
