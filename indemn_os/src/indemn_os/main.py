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
    from indemn_os.diagnose_commands import diagnose_app
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
    app.add_typer(diagnose_app, name="diagnose")
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
        "Trace",
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
    # Typer subcommand name stays singular (e.g. `indemn slackmessage list`).
    cli_name = name.lower()
    # URL slug is the entity's actual collection — Bug #48. The kernel's
    # `_route_slug_for` honors `--collection-name` overrides, so SlackMessage
    # routes to `/api/slack_messages/` even though the CLI subcommand stays
    # `slackmessage`. Falls back to naive plural for backward compatibility
    # with older API instances that don't populate the `collection` meta
    # field yet.
    slug = meta.get("collection") or (cli_name + "s")
    entity_app = typer.Typer(name=cli_name, help=f"{name} operations")

    @entity_app.command("list")
    def list_cmd(
        limit: int = 20,
        offset: int = 0,
        status: str = None,
        data: str = typer.Option(
            None,
            "--data",
            help='JSON filter by fields, e.g. \'{"company":"69eb..."}\'',
        ),
        fmt: str = typer.Option("json", "--format"),
    ):
        """List entities with filters."""
        params = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        if data:
            params["filter"] = data
        result = client.get(f"/api/{slug}/", params=params)
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
        result = client.get(f"/api/{slug}/{entity_id}", params=params)
        render(result, fmt)

    @entity_app.command("create")
    def create_cmd(data: str = typer.Option(..., "--data")):
        """Create entity. Data as JSON string."""
        import orjson

        result = client.post(f"/api/{slug}/", json=orjson.loads(data))
        render(result, "json")

    @entity_app.command("update")
    def update_cmd(entity_id: str, data: str = typer.Option(..., "--data")):
        """Update entity fields."""
        import orjson

        result = client.put(f"/api/{slug}/{entity_id}", json=orjson.loads(data))
        render(result, "json")

    if meta.get("state_machine"):

        @entity_app.command("transition")
        def transition_cmd(entity_id: str, to: str = typer.Option(..., "--to"), reason: str = None):
            """Transition entity state."""
            result = client.post(
                f"/api/{slug}/{entity_id}/transition",
                json={"to": to, "reason": reason},
            )
            render(result, "json")

    @entity_app.command("reprocess")
    def reprocess_cmd(
        entity_id: str,
        role: str = typer.Option(..., "--role", help="Role whose watch should fire"),
        event_type: str = typer.Option(
            "created",
            "--event-type",
            help="Event type to simulate (default: 'created'; use 'transitioned:<state>' or 'method:<name>' for non-creation watches)",
        ),
    ):
        """Re-emit a message for this entity to a specific role's queue.

        Bug #10 — backfill historical entities against newly-added watches.
        The named role MUST already have a watch on this entity type matching
        the event type, otherwise the request 400s with the role's actual
        watches listed.
        """
        result = client.post(
            f"/api/{slug}/{entity_id}/reprocess",
            json={"role": role, "event_type": event_type},
        )
        render(result, "json")

    @entity_app.command("delete")
    def delete_cmd(
        entity_id: str,
        yes: bool = typer.Option(False, "--yes", help="Skip confirmation prompt"),
    ):
        """Delete a single entity by id (Bug #2).

        Hard-deletes the document. Routes through `bulk-delete` with a
        single-_id filter so it goes through the same kernel path as every
        other delete (audit trail in the changes collection, watch
        evaluation for the `deleted` event). Use this for one-off cleanups
        where `bulk-delete --filter ...` is overkill.

        For entities that have a state machine, prefer
        `indemn {entity} transition <id> --to <terminal-state>` — that
        keeps the audit trail intact and signals intent more clearly than
        a hard delete.
        """
        if not yes:
            confirm = typer.confirm(
                f"Hard-delete {cli_name} {entity_id}? This is irreversible.",
                default=False,
            )
            if not confirm:
                typer.echo("Cancelled.")
                raise typer.Exit(0)
        result = client.post(
            f"/api/{slug}/bulk",
            json={
                "operation": "delete",
                "filter_query": {"_id": entity_id},
                "dry_run": False,
            },
        )
        render(result, "json")

    # Register capability commands.
    # NOTE: closure values (cap_name, slug_name) are bound via factory
    # functions rather than default-parameter capture. The default-param
    # idiom (e.g. `_cap=cap["name"]`) leaks the params into Typer's option
    # parser and renders them as `---cap` / `---slug` in --help (Bug #5),
    # because Typer treats every function parameter as a CLI option,
    # underscored or not. Factories close over the values without exposing
    # them on the command's signature.
    # MUST stay in sync with kernel/capability/__init__.py::COLLECTION_LEVEL_CAPABILITIES.
    # Capabilities listed here are routed to /api/{collection}/{cap} (collection-level);
    # everything else routes to /api/{collection}/{id}/{cap} (entity-level) and the
    # CLI requires an entity_id.
    _COLLECTION_LEVEL_CAPS = {"fetch_new", "entity_resolve"}

    def _make_entity_cap_cmd(cap_name: str, slug_name: str):
        capability_kebab = cap_name.replace("_", "-")

        def cap_cmd(
            entity_id: str = typer.Argument(
                None, help="Entity ObjectId. Omit to apply to ALL entities of this type."
            ),
            auto: bool = typer.Option(
                False, help="Try configured rules first; LLM fallback only if needed."
            ),
            data: str = typer.Option(
                None,
                help=(
                    "JSON body for the capability. Shape depends on the "
                    f"capability — see `indemn skill get {slug_name.capitalize()}`."
                ),
            ),
        ):
            """Invoke a capability on an entity (or all if no ID given)."""
            import orjson

            params = {"auto": "true"} if auto else {}
            body = orjson.loads(data) if data else {}

            if entity_id:
                result = client.post(
                    f"/api/{slug_name}/{entity_id}/{capability_kebab}",
                    json=body,
                    params=params,
                )
                render(result, "json")
            else:
                entities = client.get(f"/api/{slug_name}/", params={"limit": 1000})
                processed = 0
                for entity in entities:
                    eid = entity.get("_id") or entity.get("id")
                    if not eid:
                        continue
                    result = client.post(
                        f"/api/{slug_name}/{eid}/{capability_kebab}",
                        json=body,
                        params=params,
                    )
                    if result.get("matched") or result.get("result"):
                        processed += 1
                typer.echo(f"Processed {processed}/{len(entities)} {slug_name}")

        return cap_cmd

    def _make_collection_cap_cmd(cap_name: str, slug_name: str):
        capability_kebab = cap_name.replace("_", "-")

        def collection_cap_cmd(
            data: str = typer.Option(
                None,
                help=(
                    f"JSON body for {cap_name}. Shape varies by capability — "
                    f"e.g. `fetch_new` accepts {{\"since\": \"<ISO 8601>\", "
                    "\"user_emails\": [\"x@y\"], \"limit\": <int>}}. "
                    f"See `indemn skill get {slug_name.capitalize()}`."
                ),
            ),
        ):
            """Invoke a collection-level capability (e.g., fetch-new)."""
            import orjson

            body = orjson.loads(data) if data else {}
            result = client.post(
                f"/api/{slug_name}/{capability_kebab}", json=body
            )
            render(result, "json")

        return collection_cap_cmd

    for cap in meta.get("capabilities", []):
        if cap["name"] in _COLLECTION_LEVEL_CAPS:
            continue  # Handled below
        cap_slug = cap["name"].replace("_", "-")
        entity_app.command(cap_slug)(_make_entity_cap_cmd(cap["name"], slug))

    # Collection-level capability commands (no entity_id — creates entities)
    for cap in meta.get("capabilities", []):
        if cap["name"] not in _COLLECTION_LEVEL_CAPS:
            continue
        cap_slug = cap["name"].replace("_", "-")
        entity_app.command(cap_slug)(_make_collection_cap_cmd(cap["name"], slug))

    # Register bulk commands for this entity type. Pass the URL slug
    # (entity's collection name, e.g. `slack_messages`) so bulk commands
    # build URLs like `/api/slack_messages/bulk` that match the actual route.
    from indemn_os.bulk_commands import register_bulk_commands

    register_bulk_commands(name, entity_app, url_slug=slug)

    # The Typer subcommand name is the singular CLI verb (e.g. `slackmessage`),
    # not the URL slug — operators invoke `indemn slackmessage list`, not
    # `indemn slack_messages list`.
    parent.add_typer(entity_app, name=cli_name)
