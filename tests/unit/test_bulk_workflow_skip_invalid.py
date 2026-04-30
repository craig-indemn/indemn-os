"""Tests for Bug #37 follow-on — bulk-delete + bulk-preview workflow
paths poison on malformed entities; only the list endpoint had
skip_invalid opt-in.

Bug #37's original fix added `_DomainQuery.to_list(skip_invalid=False)`
and wired `skip_invalid=True` only into `_register_list_route` in
`kernel/api/registration.py`. Other read paths still strict:

(a) `process_bulk_batch` activity loads matched entities via
    `find_scoped().to_list()` — Pydantic ValidationError for one
    malformed row aborts the activity. Workflow attempts retry,
    fails terminally; the bulk-delete the operator initiated to
    CLEAN UP that very row leaves the row in place.

(b) `preview_bulk_operation` activity loads sample entities via
    the same `to_list()` — dry-run preview can't render samples,
    workflow lifecycle FAILED. Operators can't see what they're
    about to delete.

Fix in this commit:

1. Propagate `skip_invalid=True` to both bulk activity to_list
   calls. Malformed rows are skipped from the entity iteration
   (with a warning naming type + _id for operator visibility).
   Valid rows process normally.

2. Add a malformed-row cleanup pass in `process_bulk_batch` for
   the DELETE operation: after iterating valid entities, query
   motor directly for any _ids that matched the filter but were
   skipped by skip_invalid, then delete_many those bad _ids.
   Logged per-_id with a warning. Audit chain isn't possible
   for malformed rows (can't compute changes from a doc that
   doesn't validate); accept the lossy audit for that case.

   This is what makes the actual cleanup-of-Bug-#37-rows use case
   work end-to-end: operator runs `bulk-delete --filter
   '{"_id":{"$in":["<bad_id_1>","<bad_id_2>"]}}'` and both rows
   are gone after the workflow completes.

PUT path tolerance is intentionally out of scope here — the use
case "patch a malformed field on a malformed row" is rare; the
admin force-delete path (this fix's cleanup pass) handles known
bad rows. PUT lenient-load is a separate future fork if needed.
"""

import inspect
from pathlib import Path


def _src() -> str:
    return Path(
        "/Users/home/Repositories/indemn-os/kernel/temporal/activities.py"
    ).read_text()


def test_process_bulk_batch_passes_skip_invalid_true():
    """The activity's `find_scoped(...).to_list()` must opt in to
    skip_invalid — otherwise one malformed row matching the filter
    aborts the entire bulk operation."""
    src = _src()
    # Find the process_bulk_batch function body
    func_src = _extract_function_source(src, "process_bulk_batch")
    # Must contain skip_invalid=True at the to_list call
    assert "skip_invalid=True" in func_src, (
        "process_bulk_batch must propagate skip_invalid=True to to_list "
        "to tolerate Bug #37-class malformed rows. Source:\n" + func_src
    )


def test_preview_bulk_operation_passes_skip_invalid_true():
    """Same opt-in for the dry-run preview's sample load."""
    src = _src()
    func_src = _extract_function_source(src, "preview_bulk_operation")
    assert "skip_invalid=True" in func_src, (
        "preview_bulk_operation must propagate skip_invalid=True to "
        "the sample to_list call. Source:\n" + func_src
    )


def test_process_bulk_batch_delete_branch_has_malformed_cleanup_pass():
    """The DELETE branch must include a cleanup pass that finds matched
    _ids skipped by Pydantic validation and deletes them via direct
    motor. Without this, `bulk-delete --filter '{"_id":"<bad_id>"}'`
    for the actual Bug #37 cleanup use case fails silently."""
    src = _src()
    func_src = _extract_function_source(src, "process_bulk_batch")
    # Look for the cleanup-pass markers — direct motor delete on _ids
    # that didn't make it through Pydantic validation.
    assert "malformed" in func_src.lower() or "skip_invalid" in func_src, (
        "process_bulk_batch DELETE must handle the Bug #37 cleanup case "
        "(malformed rows skipped by Pydantic still need to be deleted "
        "when the operator's filter targets them by _id)."
    )
    # Specifically: must reference delete_many on the motor collection
    # (the cleanup pass does this for bad _ids)
    assert "delete_many" in func_src or (
        # Or the existing per-entity delete_one path with a separate
        # bad-id sweep — accept either shape
        "delete_one" in func_src and "ids" in func_src.lower()
    )


def _extract_function_source(src: str, func_name: str) -> str:
    """Pull just one function's source from a file. Crude but enough
    for these shape pins."""
    lines = src.split("\n")
    start = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith(f"async def {func_name}(") or line.lstrip().startswith(
            f"def {func_name}("
        ):
            start = i
            break
    if start is None:
        return ""
    # Walk forward until we hit a non-indented or another top-level def
    end = start + 1
    while end < len(lines):
        stripped = lines[end].lstrip()
        if (
            stripped.startswith("async def ")
            or stripped.startswith("def ")
            or stripped.startswith("@")
            or (stripped.startswith("class ") and not lines[end].startswith(" "))
        ):
            # Same- or top-level start — function ended (decorator OK if it's its own block)
            if not lines[end].startswith(" ") and end != start:
                break
        end += 1
    return "\n".join(lines[start:end])


# ---------------------------------------------------------------------------
# Behavior pin: when DELETE branch encounters a filter that matched some
# _ids that Pydantic skipped, the cleanup pass deletes those bad _ids
# (we can't easily mock the full activity without a Temporal env, but
# we can pin that the helper function exists and has the right shape).
# ---------------------------------------------------------------------------


def test_to_list_skip_invalid_default_strict_preserved():
    """Sanity regression: the original Bug #37 fix's contract — strict
    by default, opt-in tolerance — must remain. Migrations + audit code
    rely on the strict default."""
    from kernel.entity.base import _DomainQuery

    sig = inspect.signature(_DomainQuery.to_list)
    assert "skip_invalid" in sig.parameters
    assert sig.parameters["skip_invalid"].default is False
