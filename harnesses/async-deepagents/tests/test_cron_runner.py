"""Tests for Bug #40 — deterministic scheduled-actor execution path.

The cron_runner mode bypasses the deepagents agent entirely for trivial
cron-fired CLI executions (Email/Meeting/Drive/Slack-Fetcher). The actor's
first skill carries a literal `## Command` CLI line; the harness shell-execs
it directly with no LLM in the loop.

These tests pin three concerns:
    1. The `## Command` parser — extracts argv from skill markdown, rejects
       malformed shapes, restricts execution to the indemn CLI.
    2. The run_cron_skill executor — happy path marks complete; non-zero
       exit, adapter errors, missing skill, and config errors all mark the
       message failed.
    3. The process_with_associate routing — mode=cron_runner skips the
       agent build and delegates to run_cron_skill.

Per Bug #19 follow-on, these tests construct real Pydantic instances of
AgentExecutionInput rather than SimpleNamespace stand-ins, so the
serialization/coercion paths are exercised the same way they will be in
production activity invocations.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Mirror the import setup used by sibling test_load_message_context.py — stub
# the harness-runtime modules that aren't available in the kernel test venv
# (deepagents, langchain, the harness package, harness_common). Do NOT stub
# indemn_os — AgentExecutionInput / AgentExecutionResult are real Pydantic
# classes we want to exercise.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for mod in [
    "deepagents",
    "harness",
    "harness.agent",
    "harness.completion_logic",
    "harness_common",
    "harness_common.backend",
    "harness_common.runtime",
    "langchain",
    "langchain.chat_models",
]:
    sys.modules.setdefault(mod, MagicMock())

# `harness_common.runtime.RUNTIME_ID` is read at module load — provide a stub.
sys.modules["harness_common.runtime"].RUNTIME_ID = "test-runtime"

# Stub harness_common.cli with a real CLIError class + a placeholder indemn
# the tests will monkeypatch per-case.
import types  # noqa: E402

_cli_stub = types.ModuleType("harness_common.cli")


class _StubCLIError(RuntimeError):
    pass


_cli_stub.CLIError = _StubCLIError
_cli_stub.indemn = lambda *a, **kw: None  # tests will monkeypatch
sys.modules["harness_common.cli"] = _cli_stub

import pytest  # noqa: E402

from indemn_os.types import AgentExecutionInput, AgentExecutionResult  # noqa: E402

# Now safe to import the module under test
import cron_runner  # noqa: E402
from cron_runner import (  # noqa: E402
    CronSkillConfigError,
    parse_command_from_skill,
    run_cron_skill,
)


# ---------------------------------------------------------------------------
# Parser — `## Command` section extraction
# ---------------------------------------------------------------------------


def test_parser_extracts_argv_from_bash_fence():
    """Canonical case: `## Command\\n\\n```bash\\nindemn email fetch-new\\n```"""
    content = """# Email Fetcher

Some descriptive text.

## Command

```bash
indemn email fetch-new
```

## Why this exists

Notes…
"""
    assert parse_command_from_skill(content) == ["indemn", "email", "fetch-new"]


def test_parser_extracts_argv_from_sh_fence():
    """Permissive on `sh` fence language too."""
    content = "## Command\n\n```sh\nindemn document fetch-new\n```\n"
    assert parse_command_from_skill(content) == ["indemn", "document", "fetch-new"]


def test_parser_extracts_argv_from_unlabeled_fence():
    """Permissive on bare ``` fence (no language tag)."""
    content = "## Command\n\n```\nindemn meeting fetch-new\n```\n"
    assert parse_command_from_skill(content) == ["indemn", "meeting", "fetch-new"]


def test_parser_handles_command_heading_case_insensitive():
    """`## command` (lowercase) and `## COMMAND` should also work."""
    for heading in ["## command", "## COMMAND", "## Command"]:
        content = f"{heading}\n\n```bash\nindemn email fetch-new\n```\n"
        assert parse_command_from_skill(content) == ["indemn", "email", "fetch-new"]


def test_parser_strips_inline_comment_lines_in_fence():
    """A skill author can leave `# notes` in the fence; parser ignores them."""
    content = """## Command

```bash
# This runs every cron tick — see Bug #40
indemn slackmessage fetch-new
```
"""
    assert parse_command_from_skill(content) == ["indemn", "slackmessage", "fetch-new"]


def test_parser_preserves_quoted_arguments():
    """shlex semantics — quoted args stay one arg even with spaces inside."""
    content = '## Command\n\n```bash\nindemn email fetch-new --data \'{"limit": 5}\'\n```\n'
    argv = parse_command_from_skill(content)
    assert argv == ["indemn", "email", "fetch-new", "--data", '{"limit": 5}']


def test_parser_rejects_skill_without_command_section():
    content = "# Some Skill\n\nNo command section here.\n"
    with pytest.raises(CronSkillConfigError, match="missing required `## Command`"):
        parse_command_from_skill(content)


def test_parser_rejects_empty_command_fence():
    content = "## Command\n\n```bash\n\n```\n"
    with pytest.raises(CronSkillConfigError, match="empty"):
        parse_command_from_skill(content)


def test_parser_rejects_command_only_comments():
    content = "## Command\n\n```bash\n# just a comment\n# nothing executable\n```\n"
    with pytest.raises(CronSkillConfigError, match="empty"):
        parse_command_from_skill(content)


def test_parser_rejects_multiple_commands_in_fence():
    content = """## Command

```bash
indemn email fetch-new
indemn meeting fetch-new
```
"""
    with pytest.raises(CronSkillConfigError, match="2 command lines"):
        parse_command_from_skill(content)


def test_parser_rejects_command_not_starting_with_indemn():
    """Security pin: cron_runner only runs the indemn CLI. Arbitrary shell is
    rejected so a malicious or buggy skill can't `rm -rf /` or `curl evil.sh`."""
    for bad_cmd in [
        "rm -rf /tmp",
        "curl https://example.com",
        "echo hello",
        "/bin/sh",
        "python -c 'import os'",
    ]:
        content = f"## Command\n\n```bash\n{bad_cmd}\n```\n"
        with pytest.raises(CronSkillConfigError, match="must start with `indemn`"):
            parse_command_from_skill(content)


# ---------------------------------------------------------------------------
# Executor — happy path
# ---------------------------------------------------------------------------


def _make_input(entity_type: str = "_scheduled", entity_id: str = "actor1") -> AgentExecutionInput:
    """Build a real AgentExecutionInput Pydantic instance (not SimpleNamespace).
    Per Bug #19 follow-on — exercise the real serialization path."""
    return AgentExecutionInput(
        message_id="msg1",
        associate_id="actor1",
        entity_type=entity_type,
        entity_id=entity_id,
        correlation_id="corr1",
        depth=0,
    )


def test_run_cron_skill_marks_complete_on_clean_response(monkeypatch):
    """Happy path: cron_runner loads skill, execs `indemn email fetch-new`,
    adapter returns `{fetched, created, ...}` with no errors → queue complete."""
    skill_content = "## Command\n\n```bash\nindemn email fetch-new\n```\n"
    cli_calls = []

    def fake_indemn(*args, **kwargs):
        cli_calls.append(args)
        if args == ("skill", "get", "email-fetcher"):
            return {"name": "email-fetcher", "content": skill_content}
        if args == ("email", "fetch-new"):
            return {"fetched": 12, "created": 5, "skipped_duplicates": 7, "errors": []}
        if args == ("queue", "complete", "msg1"):
            return {"status": "completed"}
        raise AssertionError(f"unexpected indemn call: {args}")

    monkeypatch.setattr(cron_runner, "indemn", fake_indemn)

    associate = {
        "_id": "actor1",
        "name": "Email Fetcher",
        "skills": ["email-fetcher"],
        "mode": "cron_runner",
    }
    result = run_cron_skill(_make_input(), associate)

    assert isinstance(result, AgentExecutionResult)
    assert result.status == "complete"
    assert result.iterations == 1
    assert result.tools_used == ["email"]
    assert result.error is None
    # Verify the queue mark fired exactly once and was `complete`
    queue_calls = [c for c in cli_calls if c[0] == "queue"]
    assert queue_calls == [("queue", "complete", "msg1")]


# ---------------------------------------------------------------------------
# Executor — failure modes
# ---------------------------------------------------------------------------


def test_run_cron_skill_fails_on_non_synthetic_entity_type(monkeypatch):
    """cron_runner is only valid on `_*` synthetic events. A watch-driven
    Email message accidentally routed here is a config error → fail message."""
    fail_calls = []

    def fake_indemn(*args, **kwargs):
        if args[0] == "queue" and args[1] == "fail":
            fail_calls.append(args)
            return {}
        raise AssertionError(
            f"only `queue fail` should be called for non-synthetic trigger; got {args}"
        )

    monkeypatch.setattr(cron_runner, "indemn", fake_indemn)

    associate = {"_id": "actor1", "name": "Misrouted", "skills": ["email-fetcher"]}
    input_ = _make_input(entity_type="Email", entity_id="email1")  # NOT `_*`

    result = run_cron_skill(input_, associate)

    assert result.status == "failed"
    assert "non-synthetic" in result.error
    assert len(fail_calls) == 1
    assert fail_calls[0][:3] == ("queue", "fail", "msg1")


def test_run_cron_skill_fails_on_empty_skills_list(monkeypatch):
    """Actor with no skills → config error → fail message."""
    fail_calls = []

    def fake_indemn(*args, **kwargs):
        if args[0] == "queue" and args[1] == "fail":
            fail_calls.append(args)
            return {}
        raise AssertionError(f"unexpected indemn call: {args}")

    monkeypatch.setattr(cron_runner, "indemn", fake_indemn)

    associate = {"_id": "actor1", "name": "No Skills", "skills": []}
    result = run_cron_skill(_make_input(), associate)

    assert result.status == "failed"
    assert "no skills assigned" in result.error
    assert len(fail_calls) == 1


def test_run_cron_skill_fails_on_skill_load_clierror(monkeypatch):
    """If `indemn skill get <name>` itself fails, surface as message failure."""
    fail_calls = []

    def fake_indemn(*args, **kwargs):
        if args == ("skill", "get", "email-fetcher"):
            raise _StubCLIError("CLI failed (1): skill not found")
        if args[0] == "queue" and args[1] == "fail":
            fail_calls.append(args)
            return {}
        raise AssertionError(f"unexpected indemn call: {args}")

    # Replace BOTH the stubbed CLIError reference in cron_runner's namespace
    # AND the indemn helper, so the except clause matches.
    monkeypatch.setattr(cron_runner, "CLIError", _StubCLIError)
    monkeypatch.setattr(cron_runner, "indemn", fake_indemn)

    associate = {"_id": "actor1", "name": "EF", "skills": ["email-fetcher"]}
    result = run_cron_skill(_make_input(), associate)

    assert result.status == "failed"
    assert "Failed to load skill" in result.error
    assert len(fail_calls) == 1


def test_run_cron_skill_fails_on_unparseable_skill(monkeypatch):
    """Skill content has no `## Command` section → config error → fail message."""
    fail_calls = []

    def fake_indemn(*args, **kwargs):
        if args == ("skill", "get", "broken"):
            return {"content": "# Just a heading. No command section.\n"}
        if args[0] == "queue" and args[1] == "fail":
            fail_calls.append(args)
            return {}
        raise AssertionError(f"unexpected indemn call: {args}")

    monkeypatch.setattr(cron_runner, "indemn", fake_indemn)

    associate = {"_id": "actor1", "name": "Broken", "skills": ["broken"]}
    result = run_cron_skill(_make_input(), associate)

    assert result.status == "failed"
    assert "parse error" in result.error
    assert len(fail_calls) == 1


def test_run_cron_skill_fails_when_command_exits_nonzero(monkeypatch):
    """CLIError from the actual fetch-new exec → fail message."""
    skill_content = "## Command\n\n```bash\nindemn email fetch-new\n```\n"
    fail_calls = []

    def fake_indemn(*args, **kwargs):
        if args == ("skill", "get", "email-fetcher"):
            return {"content": skill_content}
        if args == ("email", "fetch-new"):
            raise _StubCLIError("CLI failed (1): adapter unreachable")
        if args[0] == "queue" and args[1] == "fail":
            fail_calls.append(args)
            return {}
        raise AssertionError(f"unexpected indemn call: {args}")

    monkeypatch.setattr(cron_runner, "CLIError", _StubCLIError)
    monkeypatch.setattr(cron_runner, "indemn", fake_indemn)

    associate = {"_id": "actor1", "name": "EF", "skills": ["email-fetcher"]}
    result = run_cron_skill(_make_input(), associate)

    assert result.status == "failed"
    assert "exit non-zero" in result.error
    assert "adapter unreachable" in result.error
    assert len(fail_calls) == 1


def test_run_cron_skill_fails_when_response_has_errors_field(monkeypatch):
    """Adapter returns ok exit but JSON has non-empty `errors` → fail message
    so dead_letter surfaces it for operator visibility."""
    skill_content = "## Command\n\n```bash\nindemn email fetch-new\n```\n"
    fail_calls = []

    def fake_indemn(*args, **kwargs):
        if args == ("skill", "get", "email-fetcher"):
            return {"content": skill_content}
        if args == ("email", "fetch-new"):
            return {
                "fetched": 5,
                "created": 3,
                "errors": [{"user": "kyle@indemn.ai", "error": "rate-limited"}],
            }
        if args[0] == "queue" and args[1] == "fail":
            fail_calls.append(args)
            return {}
        raise AssertionError(f"unexpected indemn call: {args}")

    monkeypatch.setattr(cron_runner, "indemn", fake_indemn)

    associate = {"_id": "actor1", "name": "EF", "skills": ["email-fetcher"]}
    result = run_cron_skill(_make_input(), associate)

    assert result.status == "failed"
    assert "1 adapter error" in result.error
    assert "rate-limited" in result.error
    assert len(fail_calls) == 1


def test_run_cron_skill_treats_missing_errors_field_as_clean(monkeypatch):
    """Adapter response without an `errors` field at all (vs. `errors: []`)
    is still a clean success — dict.get() falls back to []."""
    skill_content = "## Command\n\n```bash\nindemn email fetch-new\n```\n"

    def fake_indemn(*args, **kwargs):
        if args == ("skill", "get", "email-fetcher"):
            return {"content": skill_content}
        if args == ("email", "fetch-new"):
            return {"fetched": 0, "created": 0}  # no `errors` key at all
        if args == ("queue", "complete", "msg1"):
            return {}
        raise AssertionError(f"unexpected indemn call: {args}")

    monkeypatch.setattr(cron_runner, "indemn", fake_indemn)

    associate = {"_id": "actor1", "name": "EF", "skills": ["email-fetcher"]}
    result = run_cron_skill(_make_input(), associate)

    assert result.status == "complete"


def test_run_cron_skill_fails_if_queue_complete_fails(monkeypatch):
    """If the cron command succeeded but `queue complete` fails, surface as
    failed — the message-lifecycle truth wins over the operation truth."""
    skill_content = "## Command\n\n```bash\nindemn email fetch-new\n```\n"

    def fake_indemn(*args, **kwargs):
        if args == ("skill", "get", "email-fetcher"):
            return {"content": skill_content}
        if args == ("email", "fetch-new"):
            return {"fetched": 1, "created": 1, "errors": []}
        if args == ("queue", "complete", "msg1"):
            raise _StubCLIError("CLI failed (5): queue offline")
        raise AssertionError(f"unexpected indemn call: {args}")

    monkeypatch.setattr(cron_runner, "CLIError", _StubCLIError)
    monkeypatch.setattr(cron_runner, "indemn", fake_indemn)

    associate = {"_id": "actor1", "name": "EF", "skills": ["email-fetcher"]}
    result = run_cron_skill(_make_input(), associate)

    assert result.status == "failed"
    assert "queue complete failed" in result.error


# ---------------------------------------------------------------------------
# Pin: helper is sync (matches sibling _load_message_context)
# ---------------------------------------------------------------------------


def test_run_cron_skill_is_synchronous():
    """run_cron_skill is a sync function called from an async activity.
    Subprocess calls block the worker thread which is fine — Temporal
    activities don't require cooperative scheduling. The CALLER (in main.py)
    wraps this in `asyncio.to_thread` and runs a heartbeat loop concurrently
    so Temporal's heartbeat_timeout doesn't kill long-running fetches —
    that's the activity-level concern, not run_cron_skill's."""
    import inspect

    assert not inspect.iscoroutinefunction(run_cron_skill)


# ---------------------------------------------------------------------------
# OTEL span emission — pin that `cron_runner.run` span carries the
# associate/message/argv/result attributes that Grafana queries depend on.
# ---------------------------------------------------------------------------


# Module-level OTEL provider/exporter — OTEL only allows set_tracer_provider
# once globally per process, so we wire it once and let tests clear+reuse it.
_OTEL_EXPORTER = None


def _capture_spans():
    """Install (once) an in-memory OTEL exporter, clear any prior spans,
    and return the exporter. Tests call this at the top to reset state."""
    global _OTEL_EXPORTER
    from opentelemetry import trace as ot_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    if _OTEL_EXPORTER is None:
        _OTEL_EXPORTER = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_OTEL_EXPORTER))
        ot_trace.set_tracer_provider(provider)
        # Re-bind cron_runner's module-level tracer to pick up the new provider.
        cron_runner._tracer = ot_trace.get_tracer("cron_runner")
    else:
        _OTEL_EXPORTER.clear()
    return _OTEL_EXPORTER


def test_run_cron_skill_emits_otel_span_with_attributes(monkeypatch):
    """Pin: a successful cron_runner.run emits a `cron_runner.run` span with
    associate/message/argv/tool/result attributes. Grafana dashboards filter
    by `associate.id` (matching CLAUDE.md § 8 muscle memory) and aggregate
    on `result.fetched|created|errors_count` for throughput dashboards."""
    exporter = _capture_spans()

    skill_content = "## Command\n\n```bash\nindemn email fetch-new\n```\n"

    def fake_indemn(*args, **kwargs):
        if args == ("skill", "get", "email-fetcher"):
            return {"content": skill_content}
        if args == ("email", "fetch-new"):
            return {"fetched": 12, "created": 5, "skipped_duplicates": 7, "errors": []}
        if args == ("queue", "complete", "msg1"):
            return {}
        raise AssertionError(f"unexpected indemn call: {args}")

    monkeypatch.setattr(cron_runner, "indemn", fake_indemn)

    associate = {
        "_id": "actor1",
        "name": "Email Fetcher",
        "skills": ["email-fetcher"],
        "mode": "cron_runner",
    }
    result = run_cron_skill(_make_input(), associate)
    assert result.status == "complete"

    spans = exporter.get_finished_spans()
    cron_spans = [s for s in spans if s.name == "cron_runner.run"]
    assert len(cron_spans) == 1, f"expected 1 cron_runner.run span, got {len(cron_spans)}"
    s = cron_spans[0]
    attrs = dict(s.attributes or {})
    assert attrs.get("associate.id") == "actor1"
    assert attrs.get("associate.name") == "Email Fetcher"
    assert attrs.get("message.id") == "msg1"
    assert attrs.get("entity.type") == "_scheduled"
    assert attrs.get("argv") == "indemn email fetch-new"
    assert attrs.get("tool") == "email"
    assert attrs.get("result.fetched") == 12
    assert attrs.get("result.created") == 5
    assert attrs.get("result.skipped_duplicates") == 7
    assert attrs.get("result.errors_count") == 0
    assert attrs.get("outcome") == "complete"


def test_run_cron_skill_otel_span_records_failure_outcome(monkeypatch):
    """Pin: when cron_runner fails (e.g. CLI error), the span still emits
    with `outcome=failed` so Grafana can query failed runs by associate."""
    exporter = _capture_spans()

    skill_content = "## Command\n\n```bash\nindemn email fetch-new\n```\n"

    def fake_indemn(*args, **kwargs):
        if args == ("skill", "get", "email-fetcher"):
            return {"content": skill_content}
        if args == ("email", "fetch-new"):
            raise _StubCLIError("CLI failed (1): adapter unreachable")
        if args[0] == "queue" and args[1] == "fail":
            return {}
        raise AssertionError(f"unexpected indemn call: {args}")

    monkeypatch.setattr(cron_runner, "CLIError", _StubCLIError)
    monkeypatch.setattr(cron_runner, "indemn", fake_indemn)

    associate = {"_id": "actor1", "name": "EF", "skills": ["email-fetcher"]}
    result = run_cron_skill(_make_input(), associate)
    assert result.status == "failed"

    spans = exporter.get_finished_spans()
    cron_spans = [s for s in spans if s.name == "cron_runner.run"]
    assert len(cron_spans) == 1
    s = cron_spans[0]
    attrs = dict(s.attributes or {})
    assert attrs.get("outcome") == "failed"
    assert attrs.get("argv") == "indemn email fetch-new"
    # The CLIError exception should be recorded on the span
    assert any(e.name == "exception" for e in s.events), (
        "expected span.record_exception() to emit an exception event"
    )


# ---------------------------------------------------------------------------
# Actor mode literal — pin that "cron_runner" is a valid mode value
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Heartbeat pin — process_with_associate's cron_runner branch must heartbeat
# during the subprocess wait so Temporal's heartbeat_timeout (90s) doesn't
# kill long-running fetches. Source-level pin matches the file-level
# patterns elsewhere in this suite.
# ---------------------------------------------------------------------------


def test_process_with_associate_heartbeats_cron_runner_branch():
    """Pin the cron_runner branch in `process_with_associate` (main.py):
    must call `activity.heartbeat()` at start AND run an asyncio task that
    heartbeats periodically while the subprocess work runs in
    `asyncio.to_thread`. Pre-fix, blocking `subprocess.run` inside
    `run_cron_skill` would exceed 90s and Temporal would cancel the
    activity. The 8m15s `Activity Heartbeat timeout` failure on workflow
    msg-69f81d4a1f2c3ee82ecb65bf is the observed symptom this guards
    against."""
    import inspect

    # Read main.py source — the harness package isn't importable in this
    # test venv (deepagents stubs etc.), so source inspection of the file
    # itself is the right tool.
    main_path = Path(__file__).resolve().parents[1] / "main.py"
    src = main_path.read_text()

    # Locate the cron_runner branch
    assert 'associate.get("mode") == "cron_runner"' in src

    # Find the cron_runner branch body — everything between the `if`
    # marker and the next `# Bug #41:` comment (the watch-driven branch)
    start = src.index('associate.get("mode") == "cron_runner"')
    end = src.index("# Bug #41:", start)
    branch = src[start:end]

    # Pin: starting heartbeat fires immediately (matches agent path's
    # `activity.heartbeat("starting_agent")` shape)
    assert 'activity.heartbeat("starting_cron_runner")' in branch

    # Pin: a periodic heartbeat task runs concurrently
    assert "_cron_heartbeat_loop" in branch
    assert 'activity.heartbeat("cron_runner_running")' in branch
    assert "asyncio.create_task" in branch

    # Pin: the sync run_cron_skill is wrapped in asyncio.to_thread so the
    # heartbeat loop and subprocess wait run concurrently
    assert "asyncio.to_thread(run_cron_skill" in branch

    # Pin: the heartbeat task is cancelled in the finally block (no leak)
    assert "cron_heartbeat_task.cancel()" in branch


def test_cron_heartbeat_loop_extends_queue_visibility():
    """Bug #50 — the cron heartbeat loop must extend BOTH liveness timers:
    Temporal activity heartbeat (Bug #49) AND Mongo queue visibility
    timeout (Bug #50). Pre-fix only Temporal got heartbeated; the queue's
    5-min visibility silently expired on slow subprocesses (Email/Slack
    fetch-new on backed-up watermark, observed >5min) and the queue
    processor recovered the message mid-execution → multi-pod race →
    `complete` 404s. The fix calls `indemn queue extend-visibility
    <message_id>` on the same 30s cadence as the Temporal heartbeat."""
    main_path = Path(__file__).resolve().parents[1] / "main.py"
    src = main_path.read_text()

    # Locate the cron_runner branch
    start = src.index('associate.get("mode") == "cron_runner"')
    end = src.index("# Bug #41:", start)
    branch = src[start:end]

    # Pin: the heartbeat loop must include a queue extend-visibility call
    # alongside the Temporal heartbeat
    assert '"queue"' in branch and '"extend-visibility"' in branch, (
        "Bug #50: cron heartbeat must call `indemn queue extend-visibility` "
        "alongside the Temporal activity heartbeat"
    )
    # Pin: the call must use input.message_id (the active message) and run
    # via asyncio.to_thread so it doesn't block the asyncio loop
    assert "input.message_id" in branch
    assert "asyncio.to_thread(" in branch and "indemn," in branch
    # Pin: extend-visibility failures must not crash the heartbeat loop
    # (CLIError caught and logged — at worst we lose the race once)
    assert "except CLIError" in branch
    assert "log.warning" in branch


def test_actor_mode_literal_includes_cron_runner():
    """Pin the kernel_entities.actor.Actor.mode Literal includes the new value.
    Without this the API would 422 on `PUT /api/actors/<id> --data {"mode":"cron_runner"}`,
    blocking the production migration of the 4 fetcher actors.

    Inspects the field annotation directly rather than instantiating the
    Beanie Document — Actor is a Beanie subclass that requires
    `init_beanie` (a MongoDB-bound init) to construct, which the unit test
    venv doesn't have. The Literal annotation IS the API-validation
    contract, so checking it directly is faithful to what we need to pin."""
    import importlib
    import typing

    actor_module = importlib.import_module("kernel_entities.actor")
    Actor = actor_module.Actor

    # Actor.mode is `Optional[Literal["deterministic", "reasoning", "hybrid", "cron_runner"]]`.
    # Pydantic v2 stores annotations in model_fields.
    mode_annotation = Actor.model_fields["mode"].annotation
    # Optional[X] is Union[X, None] — pull the non-None arg.
    args = typing.get_args(mode_annotation)
    literal_arg = next(
        (a for a in args if typing.get_origin(a) is typing.Literal), None
    )
    assert literal_arg is not None, (
        f"mode annotation should be Optional[Literal[...]]; got {mode_annotation!r}"
    )

    allowed_values = set(typing.get_args(literal_arg))
    assert "cron_runner" in allowed_values, (
        f"Actor.mode Literal must include 'cron_runner' for Bug #40; "
        f"got {sorted(allowed_values)}"
    )
    # Also pin the existing values stay (no accidental removal)
    assert {"deterministic", "reasoning", "hybrid"} <= allowed_values
