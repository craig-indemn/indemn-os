"""Tests for Bug #38 — runtime task queue jammed by uncaught
WorkflowAlreadyStartedError + perpetual retry of stale workflows +
fall-through to HumanReviewWorkflow for autonomous-role messages.

Three coupled root causes, one PR (this fix):

(1) `kernel.queue_processor.dispatch_associate_workflows` catches
    `RPCError` with `RPCStatusCode.ALREADY_EXISTS`, but the temporalio
    1.25 SDK transforms `RPCError(ALREADY_EXISTS)` →
    `WorkflowAlreadyStartedError` before raising to user code (when
    `grpc_status.details` is present, which is normal operation —
    see `temporalio/client.py:8057`). `WorkflowAlreadyStartedError`
    lives in `temporalio.exceptions` and is NOT a subclass of
    `RPCError` (it inherits from `FailureError`, `TemporalError`,
    `Exception`). The kernel's `except RPCError` branch cannot catch
    it; the exception falls through to the generic `except Exception`
    handler at line 152 which logs `[WARN] Failed to dispatch
    workflow ...: Workflow execution already started` — exactly the
    log spam observed in production.

(2) Even with the right exception caught, the kernel needs to
    DISTINGUISH a still-running workflow from a terminal-failed one.
    If the existing workflow is RUNNING, the message is legitimately
    in flight and we should leave it alone. If the workflow is
    terminal (FAILED / COMPLETED / CANCELED / TERMINATED /
    TIMED_OUT), the message is ORPHANED — the workflow is gone but
    the message stayed at status=pending (because claim_message
    never ran or the harness never called `indemn queue complete`).
    Orphan messages need to transition to `dead_letter` so the
    sweep stops trying to redispatch them every cycle.

(3) Messages whose target role has `type=associate` actors but
    NONE in `status=active` (e.g., EC suspended) should NOT fall
    through to HumanReviewWorkflow — that creates work routed to
    a human queue nobody monitors. Park the message at a new
    `parked` status; dispatch query includes `parked` so the next
    sweep re-evaluates and dispatches when the associate
    reactivates. Distinguish from "no associates at all"
    (legitimate human role) which keeps the existing
    HumanReviewWorkflow fall-through.

The test file pins each of these three behaviors via behavior-style
unit tests against the extracted `_dispatch_one_message` helper +
shape-pin source greps where the structure is too coupled to mock
cleanly.
"""

import inspect
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId
from temporalio.exceptions import WorkflowAlreadyStartedError


# ---------------------------------------------------------------------------
# Shape pins: structural assertions that survive code style changes
# ---------------------------------------------------------------------------


def _src() -> str:
    return Path("/Users/home/Repositories/indemn-os/kernel/queue_processor.py").read_text()


def test_imports_workflow_already_started_error_from_temporalio_exceptions():
    """The catch handler depends on this exception class. It MUST be
    imported from `temporalio.exceptions` (not `temporalio.service`)
    because that's where the SDK 1.25 puts it. Also: must not be
    confused with `RPCError` which is a sibling class in
    `temporalio.service`."""
    src = _src()
    assert "from temporalio.exceptions import WorkflowAlreadyStartedError" in src or (
        "from temporalio import exceptions" in src
        and "WorkflowAlreadyStartedError" in src
    )


def test_dispatch_catches_workflow_already_started_error_explicitly():
    """The catch must name `WorkflowAlreadyStartedError` explicitly,
    not rely on the generic `except Exception` fallback (which loses
    information and produces noisy logs)."""
    src = _src()
    assert "except WorkflowAlreadyStartedError" in src


def test_dispatch_includes_parked_status_in_query():
    """The dispatch sweep query must include status=parked alongside
    status=pending so messages parked-by-no-active-associate get
    re-evaluated each cycle. When the operator activates the
    suspended associate, the next sweep dispatches the parked
    queue without operator intervention."""
    src = _src()
    # Look for an $in over status that includes both pending and parked
    assert "\"parked\"" in src or "'parked'" in src
    # And the find query references it in a status filter
    assert "$in" in src and "pending" in src and "parked" in src


def test_message_schema_includes_parked_literal():
    """The Message.status Literal must include 'parked' so the
    transition is type-valid."""
    schema_src = Path(
        "/Users/home/Repositories/indemn-os/kernel/message/schema.py"
    ).read_text()
    assert "\"parked\"" in schema_src


# ---------------------------------------------------------------------------
# Behavior tests for `_dispatch_one_message` (the extracted helper)
# ---------------------------------------------------------------------------


def _make_message(status: str = "pending") -> MagicMock:
    """Minimal Message stand-in. The helper only needs id, target_role,
    org_id, and a way to update + save status."""
    msg = MagicMock()
    msg.id = ObjectId()
    msg.target_role = "email_classifier"
    msg.org_id = ObjectId()
    msg.status = status
    msg.last_error = None
    return msg


def _patch_helper_deps(monkeypatch, role=None, active_assocs=None, all_assocs=None):
    """Patch Role.find_one + Actor.find at module-level for the helper.

    `role` defaults to a stub matching the message's target_role.
    `active_assocs` defaults to one active associate (so default test
    path is the happy ProcessMessageWorkflow case).
    `all_assocs` defaults to the same as active_assocs (no suspended-
    only case unless test specifies)."""
    role_obj = role if role is not None else SimpleNamespace(id=ObjectId(), name="email_classifier")
    if active_assocs is None:
        active_assocs = [SimpleNamespace(id=ObjectId(), status="active")]
    if all_assocs is None:
        all_assocs = list(active_assocs)

    # `await Role.find_one(...)` in production — patch with an
    # AsyncMock so the awaited result is the role object.
    monkeypatch.setattr(
        "kernel_entities.role.Role.find_one",
        AsyncMock(return_value=role_obj),
        raising=True,
    )

    # `await Actor.find(...).to_list()` in production — Actor.find is sync,
    # returns a query whose .to_list() is awaitable.
    class _FindResult:
        def __init__(self, items):
            self._items = items

        async def to_list(self, length=None):
            return self._items

    def _actor_find(filter_dict, *args, **kwargs):
        # Production calls Actor.find({"type":"associate","role_ids":role.id,
        #   "status":"active","org_id":...}) for active and a separate
        # call without status filter for all-associates lookup.
        if filter_dict.get("status") == "active":
            return _FindResult(active_assocs)
        return _FindResult(all_assocs)

    monkeypatch.setattr(
        "kernel_entities.actor.Actor.find",
        _actor_find,
        raising=True,
    )


@pytest.mark.asyncio
async def test_already_started_running_leaves_message_at_pending(monkeypatch, caplog):
    """If existing workflow is RUNNING, the message is legitimately in
    flight (claim_message just hasn't transitioned status yet, or the
    workflow is mid-retry). Don't touch the message; don't log a
    warning that suggests dispatch failed."""
    from temporalio.client import WorkflowExecutionStatus

    from kernel.queue_processor import _dispatch_one_message

    # Mock client.start_workflow → raises WorkflowAlreadyStartedError
    # Mock client.get_workflow_handle(...).describe() → status=RUNNING
    msg = _make_message()
    handle = MagicMock()
    handle.describe = AsyncMock(
        return_value=SimpleNamespace(status=WorkflowExecutionStatus.RUNNING)
    )

    client = MagicMock()
    client.start_workflow = AsyncMock(
        side_effect=WorkflowAlreadyStartedError(
            f"msg-{msg.id}", "ProcessMessageWorkflow", run_id="r1"
        )
    )
    client.get_workflow_handle = MagicMock(return_value=handle)

    _patch_helper_deps(monkeypatch)

    with caplog.at_level("WARNING"):
        await _dispatch_one_message(msg, client)

    # No "Failed to dispatch" warning — the catch handled cleanly
    failures = [
        r for r in caplog.records if "Failed to dispatch" in r.message
    ]
    assert failures == [], f"Unexpected warning: {[r.message for r in failures]}"

    # Message status untouched (still pending)
    assert msg.status == "pending"


@pytest.mark.asyncio
async def test_already_started_failed_marks_message_dead_letter(monkeypatch):
    """If existing workflow is in a terminal-failed state, the message
    is orphaned. Mark it dead_letter so the sweep stops re-dispatching."""
    from temporalio.client import WorkflowExecutionStatus

    from kernel.queue_processor import _dispatch_one_message

    msg = _make_message()
    handle = MagicMock()
    handle.describe = AsyncMock(
        return_value=SimpleNamespace(status=WorkflowExecutionStatus.FAILED)
    )
    # Mock the save_tracked path. _dispatch_one_message updates the message
    # via motor's update_one (no need to load through Pydantic for this
    # status-only change).
    coll = MagicMock()
    coll.update_one = AsyncMock()
    msg.get_motor_collection = MagicMock(return_value=coll)

    client = MagicMock()
    client.start_workflow = AsyncMock(
        side_effect=WorkflowAlreadyStartedError(
            f"msg-{msg.id}", "ProcessMessageWorkflow", run_id="r1"
        )
    )
    client.get_workflow_handle = MagicMock(return_value=handle)

    _patch_helper_deps(monkeypatch)

    await _dispatch_one_message(msg, client)

    # Message transitioned to dead_letter via direct motor update
    coll.update_one.assert_awaited_once()
    args = coll.update_one.await_args
    assert args[0][0] == {"_id": msg.id}
    set_clause = args[0][1].get("$set", {})
    assert set_clause.get("status") == "dead_letter"
    assert "orphaned" in (set_clause.get("last_error") or "").lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    ["COMPLETED", "FAILED", "CANCELED", "TERMINATED", "TIMED_OUT"],
)
async def test_already_started_any_terminal_status_marks_dead_letter(
    monkeypatch, terminal_status
):
    """All Temporal terminal statuses (not just FAILED) imply orphan
    messages — the workflow is gone, the message wasn't cleaned up
    by the harness. Mark dead_letter uniformly."""
    from temporalio.client import WorkflowExecutionStatus

    from kernel.queue_processor import _dispatch_one_message

    msg = _make_message()
    coll = MagicMock()
    coll.update_one = AsyncMock()
    msg.get_motor_collection = MagicMock(return_value=coll)

    handle = MagicMock()
    handle.describe = AsyncMock(
        return_value=SimpleNamespace(
            status=getattr(WorkflowExecutionStatus, terminal_status)
        )
    )
    client = MagicMock()
    client.start_workflow = AsyncMock(
        side_effect=WorkflowAlreadyStartedError(
            f"msg-{msg.id}", "ProcessMessageWorkflow", run_id="r1"
        )
    )
    client.get_workflow_handle = MagicMock(return_value=handle)
    _patch_helper_deps(monkeypatch)

    await _dispatch_one_message(msg, client)

    coll.update_one.assert_awaited_once()
    set_clause = coll.update_one.await_args[0][1].get("$set", {})
    assert set_clause.get("status") == "dead_letter"


@pytest.mark.asyncio
async def test_already_started_describe_failure_logs_and_continues(
    monkeypatch, caplog
):
    """If `handle.describe()` itself raises (e.g., Temporal connectivity
    blip, permission issue), don't crash the sweep loop. Log and move
    on; the next sweep will retry."""
    from kernel.queue_processor import _dispatch_one_message

    msg = _make_message()
    handle = MagicMock()
    handle.describe = AsyncMock(side_effect=RuntimeError("temporal flaky"))

    client = MagicMock()
    client.start_workflow = AsyncMock(
        side_effect=WorkflowAlreadyStartedError(
            f"msg-{msg.id}", "ProcessMessageWorkflow", run_id="r1"
        )
    )
    client.get_workflow_handle = MagicMock(return_value=handle)

    _patch_helper_deps(monkeypatch)

    with caplog.at_level("WARNING"):
        # Should NOT raise
        await _dispatch_one_message(msg, client)

    # Logged something about the describe failure
    assert any(
        "describe" in r.message.lower() or "status" in r.message.lower()
        for r in caplog.records
    ), f"Expected log about describe issue, got: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# Bug #38 root cause #3 — park instead of HumanReview when associates
# exist but none active
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_role_with_no_active_associates_parks_message(monkeypatch):
    """Role has type=associate actors but they're all suspended → message
    parks at status=parked. No workflow started. Re-evaluated next sweep."""
    from kernel.queue_processor import _dispatch_one_message

    msg = _make_message()
    coll = MagicMock()
    coll.update_one = AsyncMock()
    msg.get_motor_collection = MagicMock(return_value=coll)

    suspended = [SimpleNamespace(id=ObjectId(), status="suspended")]
    _patch_helper_deps(monkeypatch, active_assocs=[], all_assocs=suspended)

    client = MagicMock()
    client.start_workflow = AsyncMock()
    await _dispatch_one_message(msg, client)

    # No workflow started
    client.start_workflow.assert_not_called()

    # Message transitioned to parked
    coll.update_one.assert_awaited_once()
    set_clause = coll.update_one.await_args[0][1].get("$set", {})
    assert set_clause.get("status") == "parked"


@pytest.mark.asyncio
async def test_role_with_no_associates_at_all_falls_through_to_human_review(
    monkeypatch,
):
    """No associate actors exist for this role at all (purely human role) →
    keep the existing HumanReviewWorkflow fall-through. This is the
    `executive` / `team_member` / `reviewer` path and must not regress."""
    from kernel.queue_processor import _dispatch_one_message

    msg = _make_message()
    msg.target_role = "reviewer"
    coll = MagicMock()
    coll.update_one = AsyncMock()
    msg.get_motor_collection = MagicMock(return_value=coll)

    _patch_helper_deps(monkeypatch, active_assocs=[], all_assocs=[])

    client = MagicMock()
    client.start_workflow = AsyncMock()
    await _dispatch_one_message(msg, client)

    # HumanReviewWorkflow started — id prefix is `human-review-`
    client.start_workflow.assert_awaited_once()
    kwargs = client.start_workflow.await_args.kwargs
    assert kwargs.get("id", "").startswith("human-review-")

    # Message NOT transitioned to parked — it's been dispatched
    coll.update_one.assert_not_called()


@pytest.mark.asyncio
async def test_role_with_active_associate_dispatches_normally(monkeypatch):
    """Happy path regression — active associate = normal
    ProcessMessageWorkflow dispatch, untouched by Bug #38 fix."""
    from kernel.queue_processor import _dispatch_one_message

    msg = _make_message()
    coll = MagicMock()
    coll.update_one = AsyncMock()
    msg.get_motor_collection = MagicMock(return_value=coll)

    active = [SimpleNamespace(id=ObjectId(), status="active")]
    _patch_helper_deps(monkeypatch, active_assocs=active, all_assocs=active)

    client = MagicMock()
    client.start_workflow = AsyncMock()
    await _dispatch_one_message(msg, client)

    # ProcessMessageWorkflow started — id prefix is `msg-`
    client.start_workflow.assert_awaited_once()
    kwargs = client.start_workflow.await_args.kwargs
    assert kwargs.get("id", "").startswith("msg-")

    # No status change
    coll.update_one.assert_not_called()


# ---------------------------------------------------------------------------
# Bug #38 root cause #1 — verify the helper exists and has the right
# signature so the sweep loop can call it
# ---------------------------------------------------------------------------


def test_dispatch_one_message_exists_and_is_callable():
    """The helper must be exported from kernel.queue_processor so the
    sweep loop calls it once per pending message. Extracting this
    helper is what makes the catch behavior testable in isolation."""
    from kernel import queue_processor

    assert hasattr(queue_processor, "_dispatch_one_message")
    sig = inspect.signature(queue_processor._dispatch_one_message)
    # Takes a message + a client (and may take more positional args, but
    # those two are required)
    params = list(sig.parameters.values())
    assert len(params) >= 2
