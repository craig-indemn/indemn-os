"""Actor management CLI — convenience create, add-role, add-auth.

Replaces dynamic CRUD for Actor with ergonomic named-flag commands.
The --role flag resolves role name→ID, --owner-actor resolves email→ID.
"""

import typer

from indemn_os.client import CLIClient, render

actor_app = typer.Typer(name="actor", help="Actor management")


@actor_app.command("create")
def create_actor(
    type: str = typer.Option(..., "--type", help="human, associate, or tier3_developer"),
    name: str = typer.Option(..., "--name"),
    email: str = typer.Option(None, "--email"),
    role: str = typer.Option(None, "--role", help="Role name (resolved to ID)"),
    skills: str = typer.Option(None, "--skills", help="JSON array of skill names"),
    mode: str = typer.Option(None, "--mode", help="deterministic, reasoning, or hybrid"),
    trigger_schedule: str = typer.Option(
        None,
        "--trigger-schedule",
        help="Cron expression",
    ),
    owner_actor: str = typer.Option(
        None,
        "--owner-actor",
        help="Owner email (resolved to ID)",
    ),
):
    """Create an actor with ergonomic flags.

    Example: indemn actor create --type associate --name "Meeting Processor" \\
      --role meeting_processor --skills '["meeting-extraction"]' --mode reasoning
    """
    import orjson

    client = CLIClient()
    data = {"type": type, "name": name}

    if email:
        data["email"] = email
    if skills:
        data["skills"] = orjson.loads(skills)
    if mode:
        data["mode"] = mode
    if trigger_schedule:
        data["trigger_schedule"] = trigger_schedule

    # Resolve --role name to role_id
    if role:
        try:
            roles_resp = client.get("/api/roles/", params={"limit": 100})
            for r in roles_resp:
                if r.get("name") == role:
                    role_id = r.get("_id") or r.get("id")
                    data["role_ids"] = [role_id]
                    break
            else:
                typer.echo(f"Warning: role '{role}' not found", err=True)
        except Exception:
            typer.echo(f"Warning: could not resolve role '{role}'", err=True)

    # Resolve --owner-actor email to actor_id
    if owner_actor:
        try:
            actors_resp = client.get(
                "/api/actors/",
                params={"limit": 100},
            )
            for a in actors_resp:
                if a.get("email") == owner_actor:
                    owner_id = a.get("_id") or a.get("id")
                    data["owner_actor_id"] = owner_id
                    break
            else:
                typer.echo(
                    f"Warning: actor '{owner_actor}' not found",
                    err=True,
                )
        except Exception:
            typer.echo(
                f"Warning: could not resolve owner '{owner_actor}'",
                err=True,
            )

    result = client.post("/api/actors/", json=data)
    typer.echo(f"Created actor: {name} ({type})")
    render(result, "json")


@actor_app.command("list")
def list_actors(
    type: str = typer.Option(None, "--type", help="Filter by type"),
    status: str = typer.Option(None, "--status", help="Filter by status"),
    fmt: str = typer.Option("json", "--format"),
):
    """List actors.

    Bug #28: --type used to send `?type=...` as a top-level query param,
    which the auto-list endpoint ignored. Now sends through the standard
    `?filter={"type": "..."}` safelist (Bug #23) so the filter actually
    applies. Same for --status.
    """
    import orjson

    client = CLIClient()
    params: dict = {"limit": 100}
    if status:
        params["status"] = status
    if type:
        params["filter"] = orjson.dumps({"type": type}).decode()
    result = client.get("/api/actors/", params=params)
    render(result, fmt)


@actor_app.command("get")
def get_actor(
    actor_id: str,
    fmt: str = typer.Option("json", "--format"),
    context_profile: str = typer.Option(
        None,
        "--context-profile",
        help=(
            "Apply per-field truncation policy. Kernel entities are uncapped "
            "by design under all profiles; flag is accepted for harness compatibility."
        ),
    ),
):
    """Get an actor by ID."""
    client = CLIClient()
    params: dict = {}
    if context_profile:
        params["context_profile"] = context_profile
    result = client.get(f"/api/actors/{actor_id}", params=params)
    render(result, fmt)


@actor_app.command("update")
def update_actor(actor_id: str, data: str = typer.Option(..., "--data")):
    """Update actor fields."""
    import orjson

    client = CLIClient()
    result = client.put(f"/api/actors/{actor_id}", json=orjson.loads(data))
    render(result, "json")


@actor_app.command("transition")
def transition_actor(
    actor_id: str,
    to: str = typer.Option(..., "--to", help="Target state (e.g., active, suspended)"),
    reason: str = typer.Option(None, "--reason"),
):
    """Transition an actor's state (Bug #20 — was missing despite documented usage).

    Example: indemn actor transition <actor_id> --to suspended --reason "off duty"
    """
    client = CLIClient()
    body = {"to": to}
    if reason is not None:
        body["reason"] = reason
    result = client.post(f"/api/actors/{actor_id}/transition", json=body)
    render(result, "json")


@actor_app.command("delete")
def delete_actor(
    actor_id: str,
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation prompt"),
):
    """Delete an actor (Bug #20).

    Hard-deletes the Actor document. For most cases prefer
    `indemn actor transition <id> --to suspended` (or deprovisioned) — that
    keeps the audit trail intact. Use delete only for actors created by
    mistake or never used.
    """
    if not yes:
        confirm = typer.confirm(
            f"Hard-delete actor {actor_id}? This is irreversible. Prefer `transition --to suspended` for retired actors.",
            default=False,
        )
        if not confirm:
            typer.echo("Cancelled.")
            raise typer.Exit(0)
    client = CLIClient()
    # Routes through bulk-delete with a single-_id filter so it goes through
    # the same kernel path as every other delete (audit, watch evaluation
    # for the `deleted` event). The Bug #23 fix made $oid coercion reliable.
    result = client.post(
        "/api/actors/bulk",
        json={
            "operation": "delete",
            "filter_query": {"_id": actor_id},
            "dry_run": False,
        },
    )
    render(result, "json")


@actor_app.command("add-role")
def add_role(
    email: str,
    role: str = typer.Option(..., "--role", help="Role name to add"),
):
    """Add a role to an actor (resolved by email)."""
    client = CLIClient()
    result = client.post(
        "/api/_platform/actor/add-role",
        json={"email": email, "role_name": role},
    )
    typer.echo(f"Added role '{role}' to {email}")
    render(result, "json")


@actor_app.command("add-auth")
def add_auth(
    email: str,
    method: str = typer.Option(
        ...,
        "--method",
        help="Auth method: password, sso, magic_link",
    ),
):
    """Add an authentication method to an actor."""
    client = CLIClient()
    result = client.post(
        "/api/_platform/actor/add-auth",
        json={"email": email, "method": method},
    )
    typer.echo(f"Added auth method '{method}' to {email}")
    render(result, "json")
