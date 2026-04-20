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
