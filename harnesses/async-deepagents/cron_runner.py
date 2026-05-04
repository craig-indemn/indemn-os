"""Deterministic CLI execution path for cron-fired scheduled actors (Bug #40).

When an actor's `mode == "cron_runner"` and the message's entity_type starts
with `_` (a synthetic kernel-internal trigger like `_scheduled`), the harness
skips the deepagents agent entirely and shell-execs a literal CLI command
parsed from the actor's first skill. No LLM, no deepagents middleware, no
tool-call serialization — the skill is the program, the indemn CLI is the
runtime.

Skill format: a `## Command` heading followed by exactly one fenced code
block (```bash, ```sh, or unlabeled fence) containing one
`indemn <verb> <subverb> [args...]` line. Anything else surfaces as a
config error → permanent message failure.

Failure modes (all → `indemn queue fail`):
    - Non-synthetic entity_type — cron_runner is only valid on `_*` events.
    - Empty skills list on the actor.
    - Skill load CLIError.
    - Skill content has no parseable `## Command` section, multiple commands,
      or a command that doesn't start with `indemn`.
    - Subprocess exits non-zero (CLIError).
    - Subprocess JSON response has a non-empty `errors` field (adapter
      reported per-target failures inside an otherwise-OK exit).

Operator visibility on failure: the message lifecycle reaches `dead_letter`
after Temporal's retry budget is exhausted, surfacing in `indemn queue stats`
+ runtime logs. Auto-creating a ReviewItem on adapter `errors` is a deferred
follow-on — for v1 we keep cron_runner minimal and rely on dead_letter +
LangSmith-free runtime logs for diagnosis.
"""

import logging
import re
import shlex
from typing import Any

from harness_common.cli import CLIError, indemn
from indemn_os.types import AgentExecutionInput, AgentExecutionResult

# OTEL span emission per vision §2 item 7 — every operation is observable.
# cron_runner is a system-level execution path (not AI), so OTEL is the
# canonical observability surface (LangSmith stays for AI-agent observability).
# The activity already emits a span via TracingInterceptor in main.py; this
# adds a finer-grained `cron_runner.run` child span with associate metadata
# so Grafana queries can filter by associate_id without reading activity
# attributes — matching the muscle memory of CLAUDE.md § 8's by-associate
# debugging recipe (which queries LangSmith for agent runs).
from opentelemetry import trace

_tracer = trace.get_tracer(__name__)

log = logging.getLogger(__name__)


class CronSkillConfigError(Exception):
    """The cron_runner actor or its skill is misconfigured.

    Raised by parse_command_from_skill on shape problems. Caller (run_cron_skill)
    catches and translates to a permanent message failure — config errors should
    not be retried."""

    pass


# Match `## Command` heading followed by a fenced code block. Permissive on
# fence language tag (bash | sh | empty); body is everything up to the matching
# closing fence. Case-insensitive on the heading text so `## command` works too.
_COMMAND_SECTION_RE = re.compile(
    r"^##\s+Command\s*\n+```(?:bash|sh)?\s*\n(?P<body>.*?)\n```",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)


def parse_command_from_skill(content: str) -> list[str]:
    """Extract the cron_runner argv from skill markdown.

    Returns the argv list (e.g. `["indemn", "email", "fetch-new"]`).

    Raises CronSkillConfigError on:
        - missing `## Command` section
        - empty fence body (after stripping comments + blank lines)
        - more than one command line in the fence
        - command not starting with `indemn` (no arbitrary shell)
    """
    match = _COMMAND_SECTION_RE.search(content)
    if not match:
        raise CronSkillConfigError(
            "Skill missing required `## Command` section with a fenced code block. "
            "Expected:\n\n## Command\n\n```bash\nindemn <verb> <subverb> [args...]\n```"
        )

    body = match.group("body").strip()
    # Drop comment lines and blank lines so a skill author can leave inline notes
    lines = [
        line for line in body.split("\n")
        if line.strip() and not line.strip().startswith("#")
    ]
    if not lines:
        raise CronSkillConfigError(
            "`## Command` fence is empty (no executable line found). "
            "Expected exactly one `indemn ...` line."
        )
    if len(lines) > 1:
        raise CronSkillConfigError(
            f"`## Command` fence has {len(lines)} command lines; cron_runner v1 "
            f"supports single-command skills only. Got: {lines!r}"
        )

    argv = shlex.split(lines[0])
    if not argv or argv[0] != "indemn":
        raise CronSkillConfigError(
            f"Cron command must start with `indemn`, got: {lines[0]!r}. "
            f"cron_runner only executes the indemn CLI (no arbitrary shell allowed)."
        )

    return argv


def _fail_message(message_id: str, reason: str) -> None:
    """Mark message failed via CLI, swallowing any secondary CLIError on
    the fail call itself (we already have the original failure reason)."""
    truncated = reason[:500]
    try:
        indemn("queue", "fail", message_id, "--reason", truncated)
    except CLIError as e:
        log.warning("Failed to mark message %s failed via CLI: %s", message_id, e)


def _failure_result(reason: str, tool: str = "indemn") -> AgentExecutionResult:
    return AgentExecutionResult(
        status="failed",
        iterations=0,
        tools_used=[tool] if tool else [],
        error=reason,
    )


def run_cron_skill(
    input: AgentExecutionInput,
    associate: dict,
) -> AgentExecutionResult:
    """Deterministic execution path for cron_runner-mode actors.

    Loads the actor's first skill, parses a literal CLI command from its
    `## Command` section, subprocess-execs it (with effective_actor_id +
    causation_message_id propagated by the caller via env vars), inspects
    the JSON response for adapter-reported errors, and marks the message
    `complete` or `failed`.

    Caller (`process_with_associate`) is responsible for env-var setup +
    cleanup. This helper only owns: validate trigger → load skill → parse →
    exec → mark queue.

    Sync function, called from an async activity wrapped in
    `asyncio.to_thread` (so the caller can heartbeat concurrently). The
    subprocess.run inside the existing `indemn()` helper blocks the worker
    thread for the duration — fine because Temporal activities don't require
    cooperative scheduling, and the caller's heartbeat loop runs on the
    event-loop thread.

    OTEL: emits a `cron_runner.run` span with attributes
    `associate.id`, `associate.name`, `message.id`, `entity.type`, `argv`,
    `result.fetched`, `result.created`, `result.errors_count`, `outcome`.
    The span lives under the parent activity span (TracingInterceptor),
    so the full chain is queryable in Grafana by trace_id.
    """
    with _tracer.start_as_current_span("cron_runner.run") as span:
        span.set_attribute("associate.id", str(input.associate_id))
        span.set_attribute("associate.name", str(associate.get("name", "")))
        span.set_attribute("message.id", str(input.message_id))
        span.set_attribute("entity.type", str(input.entity_type))
        result = _run_cron_skill_inner(input, associate, span)
        span.set_attribute("outcome", result.status)
        return result


def _run_cron_skill_inner(
    input: AgentExecutionInput,
    associate: dict,
    span,
) -> AgentExecutionResult:
    """Inner implementation extracted from `run_cron_skill` so the OTEL
    span wrapper stays compact. Sets span attributes on the way through
    so partial-failure paths still produce useful traces."""
    # Cron_runner only runs on synthetic kernel-internal events. Any other
    # entity_type means a config error (e.g. a watch on Email accidentally
    # routed to a cron_runner actor) — fail loudly rather than silently
    # mis-execute.
    if not input.entity_type.startswith("_"):
        reason = (
            f"cron_runner actor {associate.get('name')!r} invoked on non-synthetic "
            f"entity_type {input.entity_type!r}; cron_runner only handles synthetic "
            f"events (entity_type starting with `_`)."
        )
        log.error(reason)
        _fail_message(input.message_id, reason)
        return _failure_result(reason, tool="")

    skills = associate.get("skills") or []
    if not skills:
        reason = (
            f"cron_runner actor {associate.get('name')!r} has no skills assigned; "
            f"need exactly one skill with a `## Command` section."
        )
        log.error(reason)
        _fail_message(input.message_id, reason)
        return _failure_result(reason, tool="")

    skill_name = skills[0]

    try:
        skill = indemn("skill", "get", skill_name)
    except CLIError as e:
        reason = f"Failed to load skill {skill_name!r}: {e}"
        log.error(reason)
        _fail_message(input.message_id, reason)
        return _failure_result(reason, tool="")

    skill_content = skill.get("content", "") if isinstance(skill, dict) else ""

    try:
        argv = parse_command_from_skill(skill_content)
    except CronSkillConfigError as e:
        reason = f"Skill {skill_name!r} parse error: {e}"
        log.error(reason)
        _fail_message(input.message_id, reason)
        return _failure_result(reason, tool="")

    # The first argv element is `indemn` itself — strip it; the indemn() helper
    # prepends the resolved binary path. Tool name for tracing/result is the
    # first verb (e.g. "email" for `indemn email fetch-new`).
    cli_args = argv[1:]
    tool_name = cli_args[0] if cli_args else "indemn"
    # OTEL: capture the parsed argv as a span attribute. Joined string keeps
    # the attribute scalar (Grafana span attributes don't render lists well).
    span.set_attribute("argv", " ".join(argv))
    span.set_attribute("tool", tool_name)

    log.info(
        "cron_runner exec: associate=%s message=%s argv=%s",
        associate.get("name"),
        input.message_id,
        argv,
    )

    try:
        result = indemn(*cli_args, timeout=600.0)
    except CLIError as e:
        reason = f"Cron command failed (exit non-zero): {e}"
        log.warning(reason)
        span.record_exception(e)
        _fail_message(input.message_id, reason)
        return _failure_result(reason, tool=tool_name)

    # Successful exit. Inspect JSON `errors` field for adapter-reported per-target
    # failures (e.g. one mailbox in a wide sweep returned an HTTP error). When
    # present, treat the whole run as a failure so operators see it via the
    # dead_letter path. Adapter-error → ReviewItem auto-creation is a deferred
    # follow-on enhancement (Bug #40 v2).
    errors: Any = []
    if isinstance(result, dict):
        errors = result.get("errors") or []
        # OTEL: capture adapter-reported result counts as scalar span attrs
        # so dashboards can query throughput by associate/entity_type.
        for k in ("fetched", "created", "skipped_duplicates"):
            v = result.get(k)
            if isinstance(v, int):
                span.set_attribute(f"result.{k}", v)
    span.set_attribute("result.errors_count", len(errors))

    if errors:
        reason = f"Cron command returned {len(errors)} adapter error(s): {str(errors)[:300]}"
        log.warning(reason)
        _fail_message(input.message_id, reason)
        return AgentExecutionResult(
            status="failed",
            iterations=1,
            tools_used=[tool_name],
            error=reason,
        )

    log.info(
        "cron_runner success: associate=%s message=%s result=%s",
        associate.get("name"),
        input.message_id,
        str(result)[:200],
    )

    try:
        indemn("queue", "complete", input.message_id)
    except CLIError as e:
        # The cron command DID succeed but we couldn't tell the queue.
        # Surface honestly as failed so the message lifecycle stays consistent
        # rather than leaving the operation stranded between exec and queue mark.
        log.error(
            "Cron command succeeded but queue complete failed for message %s: %s",
            input.message_id,
            e,
        )
        return AgentExecutionResult(
            status="failed",
            iterations=1,
            tools_used=[tool_name],
            error=f"Operation succeeded but queue complete failed: {e}",
        )

    return AgentExecutionResult(
        status="complete",
        iterations=1,
        tools_used=[tool_name],
    )
