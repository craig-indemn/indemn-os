"""Skill management CLI — create, list, get, update associate skills."""

from pathlib import Path

import typer

from indemn_os.client import CLIClient, render

skill_app = typer.Typer(name="skill", help="Skill management")


@skill_app.command("list")
def list_skills(
    type: str = typer.Option(None, "--type", help="Filter: entity or associate"),
    status: str = typer.Option("active", "--status"),
    fmt: str = typer.Option("json", "--format"),
):
    """List skills for the current org."""
    client = CLIClient()
    params = {}
    if type:
        params["type"] = type
    if status:
        params["status"] = status
    result = client.get("/api/skills/", params=params)
    render(result, fmt)


@skill_app.command("get")
def get_skill(
    name: str,
    raw: bool = typer.Option(False, "--raw", help="Show full entity JSON"),
    fmt: str = typer.Option("json", "--format"),
):
    """Get a skill by name. Returns content directly (use --raw for full entity)."""
    import os
    client = CLIClient()
    result = client.get(f"/api/skills/by-name/{name}")
    if raw or os.environ.get("INDEMN_OUTPUT_FORMAT") == "json":
        render(result, fmt, raw=True)
    elif isinstance(result, dict):
        typer.echo(result.get("content", ""))
    else:
        render(result, fmt)


@skill_app.command("create")
def create_skill(
    name: str,
    content_from_file: str = typer.Option(
        None, "--content-from-file", help="Path to markdown file"
    ),
    content: str = typer.Option(None, "--content", help="Inline markdown content"),
    skill_type: str = typer.Option("associate", "--type"),
    entity_type: str = typer.Option(None, "--entity-type"),
):
    """Create a new skill."""
    if content_from_file:
        skill_content = Path(content_from_file).read_text()
    elif content:
        skill_content = content
    else:
        typer.echo("Error: --content-from-file or --content required", err=True)
        raise typer.Exit(1)

    client = CLIClient()
    data = {
        "name": name,
        "content": skill_content,
        "type": skill_type,
    }
    if entity_type:
        data["entity_type"] = entity_type

    result = client.post("/api/skills/", json=data)
    typer.echo(f"Created skill: {name}")
    render(result, "json")


@skill_app.command("update")
def update_skill(
    name: str,
    content_from_file: str = typer.Option(
        None, "--content-from-file", help="Path to markdown file"
    ),
    content: str = typer.Option(None, "--content", help="Inline markdown content"),
):
    """Update a skill's content by name."""
    client = CLIClient()

    # Resolve name to ID
    existing = client.get(f"/api/skills/by-name/{name}")
    skill_id = existing.get("_id") or existing.get("id")
    if not skill_id:
        typer.echo(f"Skill '{name}' not found", err=True)
        raise typer.Exit(1)

    data = {}
    if content_from_file:
        data["content"] = Path(content_from_file).read_text()
    elif content:
        data["content"] = content
    else:
        typer.echo("Error: --content-from-file or --content required", err=True)
        raise typer.Exit(1)

    result = client.put(f"/api/skills/{skill_id}", json=data)
    typer.echo(f"Updated skill: {name} (v{result.get('version', '?')})")
