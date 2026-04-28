"""Bulk operation CLI commands — registered per entity type dynamically.

CLI verbs enforce selective emission discipline:
- bulk-create: emits creation events
- bulk-transition: emits state_changed events
- bulk-method: emits method_invoked events
- bulk-update: SILENT — no events (for migrations/backfills)
- bulk-delete: emits deletion events (dry-run=True by default)
"""

import typer

from kernel.cli.client import CLIClient, render


def register_bulk_commands(entity_name: str, entity_app: typer.Typer):
    """Register bulk commands for a specific entity type."""
    slug = entity_name.lower()

    @entity_app.command("bulk-create")
    def bulk_create(
        from_csv: str = typer.Option(None, "--from-csv"),
        batch_size: int = 50,
        dry_run: bool = False,
    ):
        """Create entities in bulk. Emits creation events."""
        client = CLIClient()
        result = client.post(
            f"/api/{slug}s/bulk",
            json={
                "operation": "create",
                "source_csv": from_csv,
                "batch_size": batch_size,
                "dry_run": dry_run,
            },
        )
        render(result, "json")

    @entity_app.command("bulk-transition")
    def bulk_transition(
        filter: str = typer.Option(..., "--filter", help="JSON filter query"),
        to: str = typer.Option(..., "--to"),
        batch_size: int = 50,
        dry_run: bool = False,
        failure_mode: str = "skip",
    ):
        """Transition entities in bulk. Emits state_changed events."""
        import orjson

        client = CLIClient()
        result = client.post(
            f"/api/{slug}s/bulk",
            json={
                "operation": "transition",
                "filter_query": orjson.loads(filter),
                "target_state": to,
                "batch_size": batch_size,
                "dry_run": dry_run,
                "failure_mode": failure_mode,
            },
        )
        render(result, "json")

    @entity_app.command("bulk-method")
    def bulk_method(
        method: str = typer.Option(..., "--method"),
        filter: str = typer.Option(..., "--filter", help="JSON filter query"),
        batch_size: int = 50,
        dry_run: bool = False,
        failure_mode: str = "skip",
    ):
        """Invoke @exposed method in bulk. Emits method_invoked events."""
        import orjson

        client = CLIClient()
        result = client.post(
            f"/api/{slug}s/bulk",
            json={
                "operation": "method",
                "method_name": method,
                "filter_query": orjson.loads(filter),
                "batch_size": batch_size,
                "dry_run": dry_run,
                "failure_mode": failure_mode,
            },
        )
        render(result, "json")

    @entity_app.command("bulk-update")
    def bulk_update(
        filter: str = typer.Option(..., "--filter", help="JSON filter query"),
        set_fields: str = typer.Option(..., "--set", help="JSON fields to set"),
        batch_size: int = 50,
        dry_run: bool = False,
    ):
        """Raw field updates in bulk. SILENT — no events emitted.
        Use for data migrations and backfills only.
        If changes should cascade, use bulk-method instead."""
        import orjson

        client = CLIClient()
        result = client.post(
            f"/api/{slug}s/bulk",
            json={
                "operation": "update",
                "filter_query": orjson.loads(filter),
                "sets": orjson.loads(set_fields),
                "batch_size": batch_size,
                "dry_run": dry_run,
            },
        )
        render(result, "json")

    @entity_app.command("bulk-delete")
    def bulk_delete(
        filter: str = typer.Option(..., "--filter", help="JSON filter query"),
        batch_size: int = 50,
        dry_run: bool = True,  # True by default for safety
        all_records: bool = typer.Option(
            False,
            "--all",
            help="Required for empty filter — explicit opt-in to match-all (Bug #4).",
        ),
    ):
        """Delete entities in bulk. Emits deletion events.
        Dry-run is TRUE by default for safety. `--all` is required when
        passing an empty filter to opt into match-all behavior."""
        import orjson

        client = CLIClient()
        body = {
            "operation": "delete",
            "filter_query": orjson.loads(filter),
            "batch_size": batch_size,
            "dry_run": dry_run,
        }
        if all_records:
            body["match_all"] = True
        result = client.post(f"/api/{slug}s/bulk", json=body)
        render(result, "json")
