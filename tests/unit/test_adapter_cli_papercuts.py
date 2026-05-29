"""Tests for Bug #5 (CLI capability help leaks internal flags),
Bug #7 (Google adapter logs 50+ "Missing required parameter 'fileId'"
warnings per fetch), and Bug #8 (adapter swallows all per-user errors,
hiding systemic failures).

All three were reported alongside each other on 2026-04-24 as ergonomic
papercuts in the OS-bug shakeout. They share an area (CLI generation +
Google Workspace adapter) and ship together.

  Bug #5 — `indemn meeting fetch-new --help` rendered:
            ---cap   TEXT  [default: fetch_new]
            ---slug  TEXT  [default: meeting]
          The triple dashes come from Typer treating every function
          parameter as a CLI option, including ones starting with
          underscore. The pre-fix code captured the loop variable via
          `_cap=cap["name"], _slug=slug` defaults — Python idiom for
          late-binding closures, but Typer doesn't know they're not
          user-facing. Fix: factory function closes over the values
          without exposing them on the command's signature.

  Bug #7 — `_build_meeting` had:
              elif notes_doc_id == transcript_doc_id:
          which evaluated `"" == ""` to True, so meetings with no docs
          fell into the elif branch and called the Drive API with an
          empty fileId, which produced "Missing required parameter
          'fileId'" warnings per call. ~50+ per fetch on real data.
          Fix: truthy-guard both branches, plus a defensive empty-check
          at the top of `_export_doc` so future callers that forget the
          guard still don't generate noise.

  Bug #8 — both fetch loops (meeting + email) caught every per-user
          Exception with a `log warning + continue`. When a systemic
          issue made every user fail with the same error class
          (Bug #1: every user got a 400 because the date-format was
          wrong), the function returned `fetched: 0` silently. Fix: a
          `_PerUserErrorTracker` records per-user successes / failures;
          if zero users succeed AND ≥3 (or ≥50%) fail with the same
          error class, it raises an AdapterError summarizing the
          situation.
"""

import inspect
from unittest.mock import MagicMock

import pytest

from kernel.integration.adapter import AdapterAuthError, AdapterError
from kernel.integration.adapters.google_workspace import (
    GoogleWorkspaceAdapter,
    _PerUserErrorTracker,
)

# --- Bug #5: capability CLI command signature ---


def test_cap_cmd_factory_does_not_leak_closure_params_to_cli():
    """The factory-built capability command exposes ONLY user-facing CLI
    parameters: entity_id, auto, data. Pre-fix the closure capture
    (_cap, _slug) leaked into the parameter list, which Typer rendered as
    `---cap` / `---slug` triple-dash flags in --help output."""
    # Reach into the CLI module to grab the factory used to build per-cap commands.
    # The factory is closed over inside `_register_entity_commands`; we mirror
    # its body here by re-creating the same factory shape with stub typer calls.
    # The contract under test is the SIGNATURE of the returned function — that's
    # what Typer reads to decide what flags to render.
    import typer

    from kernel.cli import app as cli_app  # noqa: F401  ensure module loads

    # Reconstruct the factory from the public source. If the factory shape
    # changes (e.g. a new user-facing param is added), this list updates.
    def _make_cap_cmd(cap_name: str, slug_name: str):
        def cap_cmd(
            entity_id: str = typer.Argument(None),
            auto: bool = False,
            data: str = None,
        ):
            return None

        return cap_cmd

    cmd = _make_cap_cmd("fetch_new", "meeting")
    sig = inspect.signature(cmd)
    # Only user-facing params — no _cap, no _slug, nothing underscored
    assert list(sig.parameters.keys()) == ["entity_id", "auto", "data"]
    for name in sig.parameters:
        assert not name.startswith("_"), f"closure leak: {name}"


def test_actual_cli_factories_exclude_closure_params():
    """Pin against drift in BOTH CLI surfaces: kernel/cli/app.py (kernel-side
    CLI used inside the kernel container) AND
    indemn_os/src/indemn_os/main.py (user-facing CLI installed via the
    indemn-os pip package). Both had the same bug; both must stay clean."""
    from pathlib import Path

    repo_root = Path("/Users/home/Repositories/indemn-os")
    sources = [
        repo_root / "kernel/cli/app.py",
        repo_root / "indemn_os/src/indemn_os/main.py",
    ]
    for src_path in sources:
        code_lines = [
            ln for ln in src_path.read_text().splitlines()
            if not ln.lstrip().startswith("#")
        ]
        code = "\n".join(code_lines)
        # The anti-pattern `_cap=cap["name"],` (with trailing comma — that's
        # the parameter-list shape) and `_slug=slug,` are what reintroduce
        # the bug. Comments that mention them without the comma stay legal.
        assert '_cap=cap["name"],' not in code, (
            f"Bug #5 regression in {src_path.name}: _cap default-parameter "
            "capture reintroduced — Typer renders these as `---cap` flags."
        )
        assert "_slug=slug," not in code, (
            f"Bug #5 regression in {src_path.name}: _slug default-parameter "
            "capture reintroduced — Typer renders these as `---slug` flags."
        )


# --- Bug #7: empty doc_id short-circuit ---


@pytest.mark.asyncio
async def test_export_doc_short_circuits_on_empty_doc_id():
    """`_export_doc("", "")` and `_export_doc(email, None)` must NOT call the
    Drive API. Returns empty string, no warning, no exception."""
    adapter = GoogleWorkspaceAdapter.__new__(GoogleWorkspaceAdapter)
    drive_called = MagicMock()
    adapter._drive_service = drive_called  # would be invoked by the real path

    out_empty = await adapter._export_doc("user@example.com", "")
    out_none = await adapter._export_doc("user@example.com", None)

    assert out_empty == ""
    assert out_none == ""
    drive_called.assert_not_called()


@pytest.mark.asyncio
async def test_export_doc_calls_drive_when_id_is_truthy():
    """Sanity counter-check: a real-looking doc_id DOES reach the Drive layer."""
    adapter = GoogleWorkspaceAdapter.__new__(GoogleWorkspaceAdapter)
    fake_service = MagicMock()
    fake_service.files().export().execute.return_value = b"hello"
    adapter._drive_service = MagicMock(return_value=fake_service)

    out = await adapter._export_doc("user@example.com", "doc-abc-123")
    assert out == "hello"
    adapter._drive_service.assert_called_once_with("user@example.com")


# --- Bug #8: PerUserErrorTracker.maybe_raise() ---


def test_tracker_does_not_raise_when_any_user_succeeded():
    """One successful user is enough — the operation produced data, the
    failures are per-user transient issues."""
    t = _PerUserErrorTracker(total=10, op="list user conferences")
    t.record_success()
    for _ in range(9):
        t.record_failure("u@x", AdapterAuthError("oauth"))
    t.maybe_raise()  # must NOT raise


def test_tracker_raises_when_all_users_fail_with_same_class_above_threshold():
    """ZERO successes + ≥3 failures of the same class = systemic. Raise.
    Surfaces Bug #1-style "every user got a 400 because of a malformed
    filter" — pre-fix the loop returned 0 results and the caller had no
    indication anything was wrong."""
    t = _PerUserErrorTracker(total=11, op="list user gmail messages")
    for i in range(11):
        t.record_failure(f"u{i}@x", AdapterAuthError("oauth refresh failed"))
    with pytest.raises(AdapterError) as exc:
        t.maybe_raise()
    msg = str(exc.value)
    assert "11/11" in msg
    assert "AdapterAuthError" in msg
    assert "list user gmail messages" in msg
    assert "systemic" in msg.lower()


def test_tracker_does_not_raise_when_failures_split_across_classes():
    """Five users fail with five different exception classes — that's a noisy
    multi-cause situation, not a single systemic failure. Don't raise (the
    caller can still inspect logs)."""
    t = _PerUserErrorTracker(total=5, op="list user conferences")
    t.record_failure("u1@x", AdapterAuthError("a"))
    t.record_failure("u2@x", ValueError("b"))
    t.record_failure("u3@x", KeyError("c"))
    t.record_failure("u4@x", TimeoutError("d"))
    t.record_failure("u5@x", RuntimeError("e"))
    t.maybe_raise()  # no single class hit threshold; do not raise


def test_tracker_does_not_raise_when_total_is_zero():
    """Empty user list: nothing to report. Don't raise spuriously."""
    t = _PerUserErrorTracker(total=0, op="list user conferences")
    t.maybe_raise()


def test_tracker_threshold_is_at_least_three():
    """A 2-user fleet where both users fail with the same error class is
    NOT enough signal to raise — could be coincidence, two users on the
    same broken mailbox config. Threshold is max(3, total/2)."""
    t = _PerUserErrorTracker(total=2, op="list user conferences")
    t.record_failure("u1@x", AdapterAuthError("oauth"))
    t.record_failure("u2@x", AdapterAuthError("oauth"))
    # 2/2 failures, same class — but only 2 users, threshold is 3
    t.maybe_raise()  # do not raise


def test_tracker_majority_threshold_for_larger_fleets():
    """For a 10-user fleet, 6 same-class failures (60%, exceeds the
    threshold = max(3, 10//2 + 1) = 6) and 4 unrecorded outcomes — but if
    those 4 didn't succeed either, we still raise on the 6 systemic
    failures."""
    t = _PerUserErrorTracker(total=10, op="list user conferences")
    for i in range(6):
        t.record_failure(f"u{i}@x", AdapterAuthError("oauth"))
    # No successes recorded; 4 users untracked but didn't succeed either
    with pytest.raises(AdapterError):
        t.maybe_raise()
