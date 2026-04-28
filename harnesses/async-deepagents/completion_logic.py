"""Detect whether an agent invocation produced useful work.

Bug #2 (silent workflow stuck-state): the harness was unconditionally calling
`indemn queue complete` after agent.ainvoke(), even when the agent produced no
output and made no mutating CLI calls. Messages then sat in `processing`
indefinitely (Apr 24 GR Little Intelligence Extractor trace).

Apr 28 follow-up (Diana@CKSpecialty trace 019dd589-8d80-7aa3-93af-b59aff572184):
the prior version had two looseness gaps that let agents "complete" without
doing anything:
  (a) it counted *attempted* mutating tool calls, not *successful* ones —
      so failed `email create` calls (E11000 dup, shell-escape errors) still
      satisfied the check
  (b) it fell back to "non-empty AI content" as a success signal — so an agent
      that just narrated "the email already exists" passed without mutating

Tightened contract: useful work requires ≥1 *successful* mutating `indemn` CLI
call. Domain-agnostic — the harness still doesn't know what each associate is
supposed to do, but "agent ran read-only commands then quit" is universally
not work. The narrative-content fallback is removed.

Per OS vision discussion 2026-04-28: the harness reports observable facts
about tool execution (this function); deeper detection of "agent didn't
fulfill its skill's intent" lives in evals (Phase E) and observability
signals on top of LangSmith traces, not in the harness.
"""

# Read-only verbs / capabilities — these calls don't change OS state.
# CRUD verbs (list, get) are the obvious ones. `entity-resolve` is a kernel
# capability that searches for candidate matches by partial-identity signals
# and returns ranked candidates without writing anything (per
# kernel/capability/entity_resolve.py — the "never auto-pick" contract).
# Add new read-only capability names here as the kernel adds them; mutating
# capabilities (auto-classify, fetch-new, stale-check, etc.) intentionally
# fall through to the mutating bucket so a successful invocation counts.
READ_ONLY_VERBS = {"list", "get", "entity-resolve"}

NO_USEFUL_WORK_REASON = (
    "agent completed without any successful state-changing CLI call "
    "(no successful create/update/transition/delete or capability invocation)"
)


def _extract_execute_command(tool_call) -> tuple[str | None, str | None]:
    """Return (tool_call_id, command) if this is an `execute` call, else (None, None)."""
    if isinstance(tool_call, dict):
        name = tool_call.get("name", "")
        args = tool_call.get("args", {})
        tc_id = tool_call.get("id")
    else:
        name = getattr(tool_call, "name", "")
        args = getattr(tool_call, "args", {})
        tc_id = getattr(tool_call, "id", None)
    if name != "execute":
        return (None, None)
    cmd = args.get("command", "") if isinstance(args, dict) else ""
    return (tc_id, cmd or "")


def _is_indemn_mutating_call(cmd: str) -> bool:
    """True if `cmd` is an `indemn` CLI invocation with a non-read-only verb."""
    parts = cmd.strip().split()
    if len(parts) < 3 or parts[0] != "indemn":
        return False
    return parts[2] not in READ_ONLY_VERBS


def _tool_call_succeeded(content: str) -> bool:
    """Did this indemn CLI call succeed?

    The local-shell backend wraps every command output with one of:
      `[Command succeeded with exit code 0]`  → success
      `[Command failed with exit code N]`     → failure
    Stderr lines are prefixed `[stderr]`. Either failure marker or any
    `[stderr]` line counts as failure (matches what an operator would
    judge looking at the trace).
    """
    if not content:
        return False
    if "[Command failed with exit code" in content:
        return False
    if "[stderr]" in content:
        return False
    return True


def _msg_attr(msg, key: str, default=""):
    """Get attribute or dict key from a message uniformly."""
    if isinstance(msg, dict):
        return msg.get(key, default)
    return getattr(msg, key, default)


def agent_did_useful_work(messages) -> tuple[bool, str | None]:
    """Did the agent make at least one successful mutating `indemn` CLI call?

    Returns (True, None) on success, or (False, reason) — the reason is
    suitable to pass as `--reason` to `indemn queue fail`.

    Walks the message list, builds tool_call_id → command for every
    mutating execute call, then checks each subsequent tool message: if its
    tool_call_id is in the mutating-call set AND the output indicates
    success, return True. Otherwise False — including the case where every
    mutating call attempted failed (today's Diana@CKSpecialty trace).
    """
    pending_mutations: dict[str, str] = {}  # tool_call_id -> command

    for msg in messages:
        msg_type = _msg_attr(msg, "type", None) or type(msg).__name__

        if msg_type == "ai":
            for tc in (_msg_attr(msg, "tool_calls", []) or []):
                tc_id, cmd = _extract_execute_command(tc)
                if tc_id and cmd and _is_indemn_mutating_call(cmd):
                    pending_mutations[tc_id] = cmd

        elif msg_type == "tool":
            tc_id = _msg_attr(msg, "tool_call_id", None)
            if not tc_id or tc_id not in pending_mutations:
                continue
            content = str(_msg_attr(msg, "content", "") or "")
            if _tool_call_succeeded(content):
                return (True, None)

    return (False, NO_USEFUL_WORK_REASON)
