"""Attention management commands — open, close, heartbeat.

Used by chat/voice harnesses for real-time session tracking.
"""

import typer

from indemn_os.client import CLIClient, render

attention_app = typer.Typer(name="attention", help="Attention (active context) management")


@attention_app.command("open")
def open_attention(
    actor_id: str = typer.Option(..., "--actor"),
    entity_type: str = typer.Option(..., "--entity-type"),
    entity_id: str = typer.Option(..., "--entity-id"),
    purpose: str = typer.Option("real_time_session", "--purpose"),
    runtime_id: str = typer.Option(None, "--runtime"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Open an Attention record (active working context)."""
    client = CLIClient()
    data = {
        "actor_id": actor_id,
        "target_entity": {"type": entity_type, "id": entity_id},
        "purpose": purpose,
    }
    if runtime_id:
        data["runtime_id"] = runtime_id
    result = client.post("/api/attentions/", json=data)
    render(result)


@attention_app.command("close")
def close_attention(
    attention_id: str,
    json_output: bool = typer.Option(False, "--json"),
):
    """Close an Attention record."""
    client = CLIClient()
    result = client.post(
        f"/api/attentions/{attention_id}/transition",
        json={"to": "closed"},
    )
    render(result)


@attention_app.command("heartbeat")
def heartbeat_attention(
    attention_id: str,
    json_output: bool = typer.Option(False, "--json"),
):
    """Send heartbeat for an Attention (keep alive)."""
    client = CLIClient()
    result = client.post(f"/api/attentions/{attention_id}/heartbeat")
    render(result)
