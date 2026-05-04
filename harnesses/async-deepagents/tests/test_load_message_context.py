"""Tests for Bug #41 — async-deepagents harness lacks `_scheduled` entity handling.

Surfaced 2026-04-30 during Bug #38 verification: kernel-side dispatch for
scheduled actors works end-to-end, but the harness's `process_with_associate`
activity unconditionally runs `indemn(entity_type.lower(), "get", entity_id, ...)`
to load the focus entity. For watch-driven messages (`Email created`,
`Meeting created`, …) this is correct — there's a real entity to load.

For scheduled (cron-fired) messages the kernel sweep at
`kernel/queue_processor.py::check_scheduled_associates` creates the message
with a SYNTHETIC entity_type:

    Message(
        entity_type="_scheduled",     # ← kernel-internal sentinel
        entity_id=associate.id,        # ← points to the Actor itself
        event_type="schedule_fired",
        ...
    )

The leading underscore is the convention for "this isn't a real entity type".
There is no `indemn _scheduled` CLI command (and there shouldn't be — the
underlying record isn't an entity). The harness running `indemn _scheduled get
<actor_id>` exits with `Error: No such command '_scheduled'`, raises
`CLIError`, the activity fails, the message goes dead_letter. Result: every
scheduled cron tick from a fetcher actor (Email-Fetcher, future
Meeting-Fetcher, Drive-Fetcher per TD-1) silently fails after dispatch.

Fix shape (framing B): extract `_load_message_context(entity_type, entity_id,
associate)` from the activity. Branch on `entity_type.startswith("_")` —
honor the kernel-internal sentinel and skip the entity-load entirely. Build a
minimal trigger-descriptor so the agent has structured context (event name,
trigger entity_id, actor name + schedule). Watch-driven messages keep the
existing entity-load behavior.

Why framing B (vs. A: changing kernel to set `entity_type="Actor"`):
- Honors the existing `_*` sentinel convention. Kernel already says
  "synthetic"; harness should listen.
- Generalizes to any future kernel-internal message types (`_circuit_broken`,
  `_zombie_recovery`, etc.) without further harness changes.
- Doesn't pretend the actor is the agent's "focus" — for scheduled work
  there is no focus entity, just a cadence + skill, and the trigger
  descriptor reflects that honestly.

The Bug #41 row in `os-learnings.md` documents this in full.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Mirror the import setup used by sibling test_agent.py — stub deepagents +
# langchain + harness_common before importing main.py so the test runs
# without the harness's runtime venv.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Stub ONLY the harness-runtime modules that aren't available in the kernel
# test venv (deepagents, langchain, the harness package itself, harness_common).
# Do NOT stub temporalio / indemn_os — they ARE installed at the kernel level
# and stubbing them with MagicMock would pollute sys.modules for later tests
# that import the real classes (cf. tests/unit/test_dispatch_workflow_already_started.py
# which imports `temporalio.exceptions.WorkflowAlreadyStartedError`).
for mod in [
    "deepagents",
    "harness",
    "harness.agent",
    "harness.completion_logic",
    "harness.cron_runner",
    "harness_common",
    "harness_common.backend",
    "harness_common.cli",
    "harness_common.runtime",
    "langchain",
    "langchain.chat_models",
]:
    sys.modules.setdefault(mod, MagicMock())

# `harness_common.runtime.RUNTIME_ID` is read at module load — provide a stub.
sys.modules["harness_common.runtime"].RUNTIME_ID = "test-runtime"

from main import _load_message_context  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic kernel-internal entity_types (Bug #41 fix): skip the indemn CLI
# load, return a trigger descriptor.
# ---------------------------------------------------------------------------


def test_scheduled_entity_type_skips_cli_load(monkeypatch):
    """The headline case: `_scheduled` from check_scheduled_associates.
    Must NOT call `indemn _scheduled get <id>` (which would CLIError)."""
    cli_calls = []

    def fake_indemn(*args, **kwargs):
        cli_calls.append(args)
        raise AssertionError(
            f"indemn CLI must not be called for synthetic entity_type; got {args}"
        )

    monkeypatch.setattr("main.indemn", fake_indemn)

    associate = {
        "_id": "69f2bf30942e5629f07a8313",
        "name": "Email Fetcher",
        "trigger_schedule": "*/5 * * * *",
    }

    context = _load_message_context(
        entity_type="_scheduled",
        entity_id="69f2bf30942e5629f07a8313",  # actor's _id (per kernel sweep)
        associate=associate,
    )

    assert cli_calls == []
    assert isinstance(context, dict)
    assert context.get("_synthetic") is True
    assert context.get("trigger") == "_scheduled"


def test_scheduled_descriptor_carries_actor_identity_and_schedule(monkeypatch):
    """The trigger descriptor must include enough for the agent's prompt:
    actor name + id + schedule, plus the trigger entity_id (= actor _id) for
    forensics. The agent's skill uses these to know what to run + log."""
    monkeypatch.setattr(
        "main.indemn",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("indemn CLI must not be called for synthetic entity_type")
        ),
    )

    associate = {
        "_id": "69f2bf30942e5629f07a8313",
        "name": "Email Fetcher",
        "trigger_schedule": "*/5 * * * *",
    }

    context = _load_message_context(
        entity_type="_scheduled",
        entity_id="69f2bf30942e5629f07a8313",
        associate=associate,
    )

    assert context["associate_name"] == "Email Fetcher"
    assert context["associate_id"] == "69f2bf30942e5629f07a8313"
    assert context["trigger_schedule"] == "*/5 * * * *"
    assert context["trigger_entity_id"] == "69f2bf30942e5629f07a8313"


def test_any_underscore_prefixed_entity_type_is_treated_as_synthetic(monkeypatch):
    """Generalization: the underscore prefix is the kernel's "synthetic"
    convention. Any future kernel-internal sentinel (`_circuit_broken`,
    `_zombie_recovery`, …) must take the same skip-load path so we don't
    have to revisit this code each time the kernel adds one."""
    monkeypatch.setattr(
        "main.indemn",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("indemn CLI must not be called for synthetic entity_type")
        ),
    )
    associate = {"_id": "act1", "name": "Hypothetical"}

    for synthetic_type in ["_scheduled", "_circuit_broken", "_zombie_recovery"]:
        context = _load_message_context(
            entity_type=synthetic_type,
            entity_id="entid",
            associate=associate,
        )
        assert context["_synthetic"] is True
        assert context["trigger"] == synthetic_type


# ---------------------------------------------------------------------------
# Watch-driven entity_types (existing behavior preserved): load the focus
# entity via the indemn CLI with --depth 2 --include-related.
# ---------------------------------------------------------------------------


def test_real_entity_type_loads_via_cli(monkeypatch):
    """Watch-driven case must continue to call
    `indemn <slug> get <id> --depth 2 --include-related`. The shape of this
    call is part of the harness contract — the agent's working set comes
    from the entity + reverse-relationship traversal."""
    cli_calls = []

    def fake_indemn(*args, **kwargs):
        cli_calls.append(args)
        return {"_id": "abc123", "subject": "test email"}

    monkeypatch.setattr("main.indemn", fake_indemn)

    context = _load_message_context(
        entity_type="Email",
        entity_id="69abc123",
        associate={"_id": "act1", "name": "Email Classifier"},
    )

    # Single CLI call with the canonical shape
    assert len(cli_calls) == 1
    args = cli_calls[0]
    assert args[0] == "email"  # lowercased
    assert args[1] == "get"
    assert args[2] == "69abc123"
    assert "--depth" in args
    assert "--include-related" in args

    # Returns the entity dict from the CLI verbatim — no synthetic wrapping
    assert context.get("_id") == "abc123"
    assert context.get("subject") == "test email"
    assert "_synthetic" not in context  # not synthetic


def test_real_entity_type_lowercases_slug(monkeypatch):
    """Pin the lowercase contract — entity_type is PascalCase (`Touchpoint`),
    CLI subcommand is lowercase (`touchpoint`)."""
    captured = {}

    def fake_indemn(*args, **kwargs):
        captured["args"] = args
        return {}

    monkeypatch.setattr("main.indemn", fake_indemn)

    _load_message_context(
        entity_type="Touchpoint",
        entity_id="69xyz",
        associate={"_id": "a1"},
    )

    assert captured["args"][0] == "touchpoint"


def test_helper_is_synchronous():
    """Pin: helper is plain function, not coroutine. Activity is async but
    this helper just routes between two CLI shapes — no await needed.
    Keeps the test surface small."""
    import inspect

    assert not inspect.iscoroutinefunction(_load_message_context)
