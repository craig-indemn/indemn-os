"""Bug #50 — visibility-recovery sweep must cap retries at max_attempts.

Pre-fix `kernel.queue_processor.check_visibility_timeouts` unconditionally
recovered timed-out `processing` messages back to `pending`, ignoring
`max_attempts`. The bus's `claim` path increments `attempt_count` on every
successful claim, but nothing capped it for the visibility-recovery loop —
so a slow-subprocess / short-visibility race could attempt the same message
7+ times indefinitely. The explicit `bus.fail` path enforces the cap; this
implicit path was the gap.

Symptom on real data 2026-05-04: stuck email_fetcher message
`69f89bec1f2c3ee82ecb66c4` at `attempt_count: 7` while `max_attempts: 3`,
last_error "Command 'indemn email fetch-new' timed out after 600.0 seconds",
status still `processing`. The visibility-recovery sweep was looping forever.

Tests pin the corrected shape via source inspection (the function's MongoDB
query structure is too coupled to mock cleanly without re-implementing the
sweep) plus a behavior test against an in-memory simulation of the two
update_many calls.
"""

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Shape pins — survive code style changes
# ---------------------------------------------------------------------------


def _src() -> str:
    return Path(
        "/Users/home/Repositories/indemn-os/kernel/queue_processor.py"
    ).read_text()


def test_check_visibility_timeouts_dead_letters_at_max_attempts():
    """The sweep MUST contain a dead-letter branch keyed on
    attempt_count >= max_attempts via $expr/$gte. Otherwise messages
    cycle infinitely between pending and processing without the
    max_attempts cap ever kicking in."""
    src = _src()
    # Find the function body
    start = src.index("async def check_visibility_timeouts")
    end = src.index("\nasync def ", start + 1)
    body = src[start:end]

    # Must have the dead-letter branch using $expr to compare attempt_count
    # against max_attempts
    assert '"$expr"' in body, (
        "check_visibility_timeouts must use $expr to compare attempt_count "
        "vs max_attempts (two-field comparison on same document)"
    )
    assert '"$gte"' in body
    assert "$attempt_count" in body
    assert "$max_attempts" in body
    assert '"status": "dead_letter"' in body, (
        "Dead-letter branch must set status='dead_letter' on capped messages"
    )


def test_check_visibility_timeouts_recovers_under_cap():
    """Below the cap, the sweep MUST still recover messages to pending
    so the queue can re-dispatch them. (Without this, the fix would
    over-correct and dead-letter every visibility timeout.)"""
    src = _src()
    start = src.index("async def check_visibility_timeouts")
    end = src.index("\nasync def ", start + 1)
    body = src[start:end]

    # Must still have the recovery branch
    assert '"status": "pending"' in body
    assert '"claimed_by": None' in body
    assert '"visibility_timeout": None' in body


def test_check_visibility_timeouts_dead_letter_and_recover_use_separate_queries():
    """The dead-letter step uses an extra `$expr` filter; the recovery
    step doesn't. Both run, in order — dead-letter first so already-capped
    messages don't sneak through to a second recovery."""
    src = _src()
    start = src.index("async def check_visibility_timeouts")
    end = src.index("\nasync def ", start + 1)
    body = src[start:end]

    # Two update_many calls
    assert body.count("update_many") == 2, (
        "check_visibility_timeouts must call update_many TWICE — once for "
        "dead_letter (capped), once for pending recovery (uncapped)"
    )

    # Dead-letter ($expr) must come BEFORE recovery (no $expr) — otherwise
    # recovery would catch capped messages too and reset their state.
    dead_letter_pos = body.find('"$expr"')
    # Find the recovery update_many — it's the one without $expr; locate
    # it by looking for the simple "status: pending" set without $expr nearby
    recovery_pos = body.find('"$set": {"status": "pending"')
    assert dead_letter_pos > 0 and recovery_pos > 0
    assert dead_letter_pos < recovery_pos, (
        "Dead-letter pass must run BEFORE recovery pass so capped messages "
        "don't get recovered back to pending"
    )


# ---------------------------------------------------------------------------
# Behavior test — actually invoke the sweep against a mock collection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_visibility_timeouts_behavior_two_update_calls(monkeypatch):
    """Behavior pin: invoking the sweep produces two distinct update_many
    calls — one with the $expr dead-letter filter, one without. Together
    with the shape pins above, this confirms both branches actually run."""
    from kernel import queue_processor

    captured_calls = []

    class FakeUpdateResult:
        modified_count = 0

    async def fake_update_many(filter_, update):
        captured_calls.append({"filter": filter_, "update": update})
        return FakeUpdateResult()

    fake_coll = MagicMock()
    fake_coll.update_many = AsyncMock(side_effect=fake_update_many)
    monkeypatch.setattr(
        queue_processor.Message,
        "get_motor_collection",
        lambda: fake_coll,
    )

    await queue_processor.check_visibility_timeouts()

    assert len(captured_calls) == 2

    # First call — dead-letter, has $expr
    first = captured_calls[0]
    assert "$expr" in first["filter"]
    assert first["update"]["$set"]["status"] == "dead_letter"

    # Second call — recovery, no $expr
    second = captured_calls[1]
    assert "$expr" not in second["filter"]
    assert second["update"]["$set"]["status"] == "pending"
    assert second["update"]["$set"]["claimed_by"] is None
    assert second["update"]["$set"]["visibility_timeout"] is None
