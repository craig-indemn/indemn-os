"""Diagnose commands — first-class CLI diagnostics for associate runs and queue messages.

Replaces the need to reach for mongosh + railway logs + aws CLI when debugging
stuck cron_runner runs, slow associates, or orphaned messages. Per os-learnings.md:
"anything the OS does, you should be able to do via CLI first."

Sub-commands:
  indemn diagnose actor <id>    — recent runs, durations, outcomes, failure modes
  indemn diagnose message <id>  — full lifecycle: claims, visibility extensions, status transitions
  indemn diagnose cron <name>   — last N cron ticks for a scheduled actor
"""

import typer

from indemn_os.client import CLIClient, render

diagnose_app = typer.Typer(name="diagnose", help="Operational diagnostics for associates and queues")


@diagnose_app.command("actor")
def diagnose_actor(
    actor_id: str,
    limit: int = typer.Option(10, "--limit"),
):
    """Recent runs for an actor — durations, outcomes, failure modes.

    Pulls from message_log (completed/failed messages targeting this actor's role)
    and shows per-run: message_id, entity_type, entity_id, status, duration,
    last_error (if failed), attempt_count.
    """
    client = CLIClient()
    result = client.get(
        f"/api/_diagnose/actor/{actor_id}",
        params={"limit": limit},
    )
    render(result)


@diagnose_app.command("message")
def diagnose_message(
    message_id: str,
):
    """Full lifecycle of a queue message.

    Shows: created_at, every claim event (claimed_by, claimed_at),
    visibility_timeout extensions, status transitions, completed_at/failed_at,
    attempt_count, last_error, correlation_id, entity context.
    """
    client = CLIClient()
    result = client.get(f"/api/_diagnose/message/{message_id}")
    render(result)


@diagnose_app.command("cron")
def diagnose_cron(
    actor_name: str,
    limit: int = typer.Option(10, "--limit"),
):
    """Last N cron ticks for a scheduled actor.

    Shows per-tick: message_id, created_at, completed_at, duration_ms,
    outcome (success/fail/dead_letter), last_error if any. Useful for
    spotting patterns (always slow? intermittent failures? stuck?).
    """
    client = CLIClient()
    result = client.get(
        "/api/_diagnose/cron",
        params={"actor_name": actor_name, "limit": limit},
    )
    render(result)
