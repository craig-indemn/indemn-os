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

import inspect
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


# Bug #4 follow-on (2026-05-25) — BulkOperationSpec schema drift on match_all.
#
# Surfaced during the eval framework refactor's P1.1 wipe of 155 old eval records.
# Symptom: every `bulk-delete --all` (and `bulk-update --all`) call put a workflow
# into `WorkflowTaskFailedCauseWorkflowWorkerUnhandledFailure` retry-loop with
# `TypeError: BulkOperationSpec.__init__() got an unexpected keyword argument
# 'match_all'`. Bulk workflows had been stuck in dev for ~4 weeks (April 27
# bulk-* workflows still showed lifecycle_status=RUNNING through May 25) because
# no one had attempted a destructive bulk-all wipe since the Bug #4 mitigation
# introduced match_all at the API + CLI layers without extending BulkOperationSpec
# at the workflow layer.


def _workflows_py_in_this_checkout() -> Path:
    """Resolve workflows.py relative to THIS test file, so the test reads from
    the current checkout (or worktree) rather than a hardcoded absolute path
    that may point at a different branch."""
    # tests/unit/test_bulk_delete_safety.py -> ../../kernel/temporal/workflows.py
    return Path(__file__).resolve().parents[2] / "kernel" / "temporal" / "workflows.py"


def test_bulk_operation_spec_declares_match_all_field():
    """Shape pin: BulkOperationSpec must declare match_all as a dataclass field
    with default False. The actual workflow-side instantiation is
    `BulkOperationSpec(**spec_dict)` at kernel/temporal/workflows.py:246, which
    fails with TypeError if any spec key isn't a field on the dataclass. The
    API + CLI layers send `match_all` in the spec dict; if this field isn't
    declared here, every `bulk-delete --all` puts the workflow into
    WorkflowTaskFailedCauseWorkflowWorkerUnhandledFailure retry-loop and the
    delete never executes (silent failure visible only via Temporal CLI describe).

    Source-string pin (vs. runtime instantiate) so this test doesn't require
    Settings env-var setup."""
    src = _workflows_py_in_this_checkout().read_text()
    # The dataclass field declaration must exist with the right default
    assert "match_all: bool = False" in src, (
        "Bug #4 follow-on regression: BulkOperationSpec.match_all field removed. "
        "Every bulk-delete --all (and bulk-update --all) will explode with "
        "TypeError at BulkOperationSpec(**spec_dict) and stick in a Temporal "
        "worker retry loop. Surfaced 2026-05-25 during eval framework refactor."
    )


def test_bulk_operation_spec_match_all_inside_dataclass_block():
    """Pin that match_all lives inside the BulkOperationSpec @dataclass block,
    not somewhere else in the module (a stray module-level var would still
    satisfy the substring check but wouldn't fix the bug)."""
    src = _workflows_py_in_this_checkout().read_text()
    # Find the dataclass block
    start = src.find("class BulkOperationSpec:")
    assert start != -1, "BulkOperationSpec class definition not found"
    # End of the dataclass = start of the next @dataclass or class
    rest = src[start:]
    end_relative = rest.find("\n@dataclass", 1)
    if end_relative == -1:
        end_relative = rest.find("\nclass ", 1)
    assert end_relative != -1, "could not bound BulkOperationSpec block"
    block = rest[:end_relative]
    assert "match_all: bool = False" in block, (
        "match_all field is in workflows.py but NOT inside the "
        "BulkOperationSpec @dataclass block — the workflow will still TypeError."
    )
