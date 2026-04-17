"""Interaction management commands — create, respond, transfer, close.

Used by chat/voice harnesses and the handoff flow.
"""

import typer

from indemn_os.client import CLIClient, render

interaction_app = typer.Typer(name="interaction", help="Interaction management")


@interaction_app.command("create")
def create_interaction(
    channel_type: str = typer.Option(..., "--channel-type", help="chat, voice, sms"),
    associate_id: str = typer.Option(None, "--associate"),
    deployment_id: str = typer.Option(None, "--deployment"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Create a new Interaction for a conversation session."""
    client = CLIClient()
    data = {"channel_type": channel_type, "status": "active"}
    if associate_id:
        data["handling_actor_id"] = associate_id
    if deployment_id:
        data["deployment_id"] = deployment_id
    result = client.post("/api/interactions/", json=data)
    render(result)


@interaction_app.command("respond")
def respond(
    interaction_id: str,
    content: str = typer.Option(..., "--content"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Submit a response to an Interaction (used by human handlers)."""
    client = CLIClient()
    result = client.post(
        f"/api/interactions/{interaction_id}/respond",
        json={"content": content},
    )
    render(result)


@interaction_app.command("transfer")
def transfer(
    interaction_id: str,
    to_actor: str = typer.Option(None, "--to-actor"),
    to_role: str = typer.Option(None, "--to-role"),
    reason: str = typer.Option(None, "--reason"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Transfer an Interaction to another actor or role (handoff)."""
    client = CLIClient()
    data = {}
    if to_actor:
        data["to_actor"] = to_actor
    if to_role:
        data["to_role"] = to_role
    if reason:
        data["reason"] = reason
    result = client.post(
        f"/api/interactions/{interaction_id}/transfer",
        json=data,
    )
    render(result)


@interaction_app.command("close")
def close_interaction(
    interaction_id: str,
    json_output: bool = typer.Option(False, "--json"),
):
    """Close an Interaction (end the conversation)."""
    client = CLIClient()
    result = client.post(
        f"/api/interactions/{interaction_id}/transition",
        json={"to": "closed"},
    )
    render(result)
