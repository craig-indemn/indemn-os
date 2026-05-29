"""Tests for Bug #4 — bulk-delete with empty filter footgun.

Pre-fix: `indemn {entity} bulk-delete --filter '{}' --no-dry-run` returned
`started → completed` with 0 deletions and no error. Pre-burst-#4 the
silent no-op was actually masking a bigger problem: the org_id contextvar
wasn't being set in the bulk activity, so `find_scoped({})` skipped its
org-scoping clause and matched nothing. Burst #4 (Bug #23) fixed the
contextvar — which made `bulk-delete --filter '{}' --no-dry-run` go from
silent-no-op to "delete every entity in the org." That's a worse
footgun.

Fix: at the API boundary in `_register_bulk_route::start_bulk`, reject
`filter_query == {}` for `delete` and `update` operations unless the
caller explicitly passes `match_all: true`. The per-entity `bulk-delete`
CLI exposes that opt-in via a `--all` flag, with help text pointing to
the dry-run default for verification.

Also: Bug #2 — singular `delete <id>` CLI command. Pre-fix only
`bulk-delete --filter '{...}'` existed for one-off deletes, returning a
workflow_id without confirmation. The new `delete` command takes an id,
prompts for confirmation (skippable with --yes), and routes through
bulk-delete with a single-_id filter so the kernel-side audit + watch
evaluation paths still run.
"""

from pathlib import Path

# Bug #4 — boundary check shape pin (the actual integration is one
# code path; pin the shape via source-grep so a future refactor that
# moves or removes the check fails the test).


def test_bulk_route_rejects_empty_filter_on_delete_without_match_all():
    """Pin: empty filter on a delete operation without `match_all: true`
    must raise HTTPException 400 at the API boundary, before the
    workflow is started."""
    src = Path(
        "/Users/home/Repositories/indemn-os/kernel/api/registration.py"
    ).read_text()
    # The shape that has to stay: empty filter + destructive op + no match_all -> 400
    assert "destructive = operation in (\"delete\", \"update\")" in src
    assert "filter_query == {}" in src
    assert 'spec.get("match_all")' in src
    # Pin the error message contains key guidance
    assert "match_all: true" in src
    assert "Empty filter on bulk" in src


def test_bulk_route_validation_runs_before_workflow_start():
    """The boundary check must happen BEFORE start_workflow — early bail-out
    so the caller sees the 400 immediately instead of having to monitor a
    workflow that fails opaquely."""
    src = Path(
        "/Users/home/Repositories/indemn-os/kernel/api/registration.py"
    ).read_text()
    # Find the start_bulk function body
    start = src.find("async def start_bulk")
    end = src.find("return {\"workflow_id\":", start)
    assert start != -1 and end != -1, "start_bulk handler shape changed"
    body = src[start:end]
    # The empty-filter check must appear in the body BEFORE the
    # start_workflow call (which is below `end`).
    raise_idx = body.find("Empty filter on bulk")
    workflow_idx = body.find("start_workflow")
    assert raise_idx != -1, "Bug #4 regression: empty-filter check removed"
    if workflow_idx != -1:
        assert raise_idx < workflow_idx, (
            "Bug #4 regression: empty-filter check moved AFTER start_workflow — "
            "callers won't see the 400, they'll see a stuck workflow"
        )


def test_match_all_flag_is_present_on_per_entity_bulk_delete():
    """Bug #4 — the `--all` flag on `bulk-delete` is the explicit opt-in
    surface for match-all. Both CLI files need it; pin both."""
    for src_path in (
        Path("/Users/home/Repositories/indemn-os/indemn_os/src/indemn_os/bulk_commands.py"),
        Path("/Users/home/Repositories/indemn-os/kernel/cli/bulk_commands.py"),
    ):
        src = src_path.read_text()
        assert "all_records: bool" in src, f"{src_path.name}: --all flag missing"
        assert '"--all"' in src, f"{src_path.name}: --all option name missing"
        assert 'body["match_all"] = True' in src, (
            f"{src_path.name}: match_all body field not wired"
        )


def test_singular_delete_command_present_on_both_cli_surfaces():
    """Bug #2 — `indemn {entity} delete <id>` exists, runs through bulk-delete
    with a single-_id filter (same kernel path as every other delete:
    audit + watch evaluation for the `deleted` event)."""
    for src_path in (
        Path("/Users/home/Repositories/indemn-os/indemn_os/src/indemn_os/main.py"),
        Path("/Users/home/Repositories/indemn-os/kernel/cli/app.py"),
    ):
        src = src_path.read_text()
        assert '@entity_app.command("delete")' in src, (
            f"{src_path.name}: singular delete command not registered"
        )
        # The single-_id filter pattern keeps audit + watch eval intact
        assert '"filter_query": {"_id": entity_id}' in src, (
            f"{src_path.name}: delete should route through bulk-delete with "
            "single-_id filter"
        )
        assert '"operation": "delete"' in src, (
            f"{src_path.name}: delete command should call delete operation"
        )


def test_singular_delete_has_confirmation_prompt():
    """Hard delete is irreversible. Both surfaces must prompt for
    confirmation (`--yes` opt-out for scripting)."""
    for src_path in (
        Path("/Users/home/Repositories/indemn-os/indemn_os/src/indemn_os/main.py"),
        Path("/Users/home/Repositories/indemn-os/kernel/cli/app.py"),
    ):
        src = src_path.read_text()
        # Find the delete command; it must have typer.confirm + --yes opt-out
        idx = src.find('@entity_app.command("delete")')
        delete_block = src[idx : idx + 1500]
        assert "typer.confirm" in delete_block, (
            f"{src_path.name}: delete missing confirmation prompt"
        )
        assert '"--yes"' in delete_block, (
            f"{src_path.name}: delete missing --yes opt-out"
        )
