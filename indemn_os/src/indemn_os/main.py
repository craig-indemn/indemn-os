"""Indemn OS CLI — entry point.

Fetches entity metadata from API and registers dynamic commands.
Static commands (platform, entity, queue) are always available.
Dynamic entity commands (submission list, email get, etc.) load from API metadata.
"""

import typer

from indemn_os.client import CLIClient, render

app = typer.Typer(name="indemn", help="Indemn OS CLI")


def main():
    """Entry point. Registers static commands, then dynamic entity commands from API."""
    # Register static commands (always available)
    from indemn_os.actor_commands import actor_app
    from indemn_os.attention_commands import attention_app
    from indemn_os.audit_commands import audit_app
    from indemn_os.auth_commands import auth_app
    from indemn_os.bulk_monitor import bulk_app
    from indemn_os.entity_commands import entity_app
    from indemn_os.events_commands import events_app
    from indemn_os.init_commands import init_app
    from indemn_os.integration_commands import integration_app
    from indemn_os.interaction_commands import interaction_app
    from indemn_os.lookup_commands import lookup_app
    from indemn_os.org_commands import org_app
    from indemn_os.platform_commands import platform_app
    from indemn_os.queue_commands import queue_app
    from indemn_os.report_commands import report_app
    from indemn_os.role_commands import role_app as role_mgmt_app
    from indemn_os.rule_commands import rule_app
    from indemn_os.runtime_commands import runtime_app
    from indemn_os.skill_commands import skill_app
    from indemn_os.trace_commands import trace_app

    app.add_typer(init_app, name="init")
    app.add_typer(auth_app, name="auth")
    app.add_typer(platform_app, name="platform")
    app.add_typer(entity_app, name="entity")
    app.add_typer(org_app, name="org")
    app.add_typer(queue_app, name="queue")
    app.add_typer(lookup_app, name="lookup")
    app.add_typer(bulk_app, name="bulk")
    app.add_typer(integration_app, name="integration")
    app.add_typer(audit_app, name="audit")
    app.add_typer(events_app, name="events")
    app.add_typer(skill_app, name="skill")
    app.add_typer(rule_app, name="rule")
    app.add_typer(actor_app, name="actor")
    app.add_typer(report_app, name="report")
    app.add_typer(role_mgmt_app, name="role")
    app.add_typer(runtime_app, name="runtime")
    app.add_typer(trace_app, name="trace")
    app.add_typer(interaction_app, name="interaction")
    app.add_typer(attention_app, name="attention")

    # Top-level deploy alias (spec: `indemn deploy --from-org --to-org`)
    @app.command("deploy")
    def deploy_alias(
        from_org: str = typer.Option(..., "--from-org"),
        to_org: str = typer.Option(..., "--to-org"),
        dry_run: bool = True,
        apply: bool = typer.Option(False, "--apply"),
    ):
        """Deploy configuration (alias for org deploy)."""
        from indemn_os.org_commands import deploy_org

        deploy_org(from_org=from_org, to_org=to_org, dry_run=dry_run, apply=apply)

    # Entities with dedicated static CLI apps — skip dynamic registration.
    # Infrastructure entities (Rule, Skill, Lookup, etc.) are also excluded
    # because they have custom routes, not auto-generated CRUD.
    _STATIC_CLI_ENTITIES = {
        "Role",
        "Actor",
        "Integration",
        "Runtime",
        "Interaction",
        "Attention",
        "Rule",
        "RuleGroup",
        "Skill",
        "Lookup",
        "EntityDefinition",
        "Message",
        "MessageLog",
        "ChangeRecord",
    }

    # Fetch entity metadata and register dynamic commands.
    # SystemExit must be caught because CLIClient._handle_error raises it on HTTP errors.
    try:
        client = CLIClient()
        meta = client.get("/api/_meta/entities")
        for entity_meta in meta:
            if entity_meta["name"] in _STATIC_CLI_ENTITIES:
                continue  # Static CLI app handles these
            _register_entity_commands(app, entity_meta, client)
    except (Exception, SystemExit):
        pass  # API unavailable — static commands still work

    app()


def _register_entity_commands(parent: typer.Typer, meta: dict, client: CLIClient):
    """Register CLI commands for one entity type. Mirrors API registration."""
    name = meta["name"]
    slug = name.lower()
    entity_app = typer.Typer(name=slug, help=f"{name} operations")

    @entity_app.command("list")
    def list_cmd(
        limit: int = 20,
        offset: int = 0,
        status: str = None,
        fmt: str = typer.Option("json", "--format"),
    ):
        """List entities with filters."""
        params = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        result = client.get(f"/api/{slug}s/", params=params)
        render(result, fmt)

    @entity_app.command("get")
    def get_cmd(
        entity_id: str,
        depth: int = typer.Option(1, "--depth", help="Resolve related entities (1-5)"),
        include_related: bool = typer.Option(False, "--include-related"),
        fmt: str = typer.Option("json", "--format"),
    ):
        """Get entity by ID with optional related entity resolution."""
        params = {}
        if depth > 1:
            params["depth"] = depth
        if include_related:
            params["include_related"] = "true"
        result = client.get(f"/api/{slug}s/{entity_id}", params=params)
        render(result, fmt)

    @entity_app.command("create")
    def create_cmd(data: str = typer.Option(..., "--data")):
        """Create entity. Data as JSON string."""
        import orjson

        result = client.post(f"/api/{slug}s/", json=orjson.loads(data))
        render(result, "json")

    @entity_app.command("update")
    def update_cmd(entity_id: str, data: str = typer.Option(..., "--data")):
        """Update entity fields."""
        import orjson

        result = client.put(f"/api/{slug}s/{entity_id}", json=orjson.loads(data))
        render(result, "json")

    if meta.get("state_machine"):

        @entity_app.command("transition")
        def transition_cmd(entity_id: str, to: str = typer.Option(..., "--to"), reason: str = None):
            """Transition entity state."""
            result = client.post(
                f"/api/{slug}s/{entity_id}/transition",
                json={"to": to, "reason": reason},
            )
            render(result, "json")

    # Register capability commands
    _COLLECTION_LEVEL_CAPS = {"fetch_new"}
    for cap in meta.get("capabilities", []):
        if cap["name"] in _COLLECTION_LEVEL_CAPS:
            continue  # Handled below
        cap_slug = cap["name"].replace("_", "-")

        @entity_app.command(cap_slug)
        def cap_cmd(
            entity_id: str = typer.Argument(None),
            auto: bool = False,
            data: str = None,
            _cap=cap["name"],
            _slug=slug,
        ):
            """Invoke a capability on an entity (or all if no ID given)."""
            import orjson

            params = {"auto": "true"} if auto else {}
            body = orjson.loads(data) if data else {}

            if entity_id:
                # Single entity
                result = client.post(
                    f"/api/{_slug}s/{entity_id}/{_cap.replace('_', '-')}",
                    json=body,
                    params=params,
                )
                render(result, "json")
            else:
                # Batch: run on all entities of this type
                entities = client.get(f"/api/{_slug}s/", params={"limit": 1000})
                processed = 0
                for entity in entities:
                    eid = entity.get("_id") or entity.get("id")
                    if not eid:
                        continue
                    result = client.post(
                        f"/api/{_slug}s/{eid}/{_cap.replace('_', '-')}",
                        json=body,
                        params=params,
                    )
                    if result.get("matched") or result.get("result"):
                        processed += 1
                typer.echo(f"Processed {processed}/{len(entities)} {_slug}s")

    # Register collection-level capability commands (no entity_id — creates entities)
    for cap in meta.get("capabilities", []):
        if cap["name"] not in _COLLECTION_LEVEL_CAPS:
            continue
        cap_slug = cap["name"].replace("_", "-")

        @entity_app.command(cap_slug)
        def collection_cap_cmd(data: str = None, _cap=cap["name"], _slug=slug):
            """Invoke a collection-level capability (e.g., fetch-new)."""
            import orjson

            body = orjson.loads(data) if data else {}
            result = client.post(
                f"/api/{_slug}s/{_cap.replace('_', '-')}", json=body
            )
            render(result, "json")

    # Register bulk commands for this entity type
    from indemn_os.bulk_commands import register_bulk_commands

    register_bulk_commands(name, entity_app)

    parent.add_typer(entity_app, name=slug)
