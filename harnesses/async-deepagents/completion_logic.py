"""Detect whether an agent invocation produced useful work.

Bug #2 (silent workflow stuck-state): the harness was unconditionally calling
`indemn queue complete` after agent.ainvoke(), even when the agent produced no
output and made no mutating CLI calls. Messages then sat in `processing`
indefinitely (Apr 24 GR Little Intelligence Extractor trace).

`agent_did_useful_work(messages)` returns (True, None) when the agent
produced state changes or a meaningful final response, or (False, reason)
when it didn't — letting the harness mark the message failed with a clear
reason instead of silent completion.

"Useful work" = at least one of:
  (a) at least one mutating `indemn` CLI call (anything but read-only verbs
      like list/get),
  (b) the final AI message has non-empty content (an actual response).
"""

READ_ONLY_VERBS = {"list", "get"}

NO_USEFUL_WORK_REASON = (
    "agent completed without producing entity state changes or meaningful output"
)


def _extract_execute_command(tool_call) -> str | None:
    """Return the command string if this tool_call invoked the `execute` tool, else None.

    Handles both dict and object forms — LangChain serializes tool_calls either way
    depending on the model and the streaming context.
    """
    if isinstance(tool_call, dict):
        name = tool_call.get("name", "")
        args = tool_call.get("args", {})
    else:
        name = getattr(tool_call, "name", "")
        args = getattr(tool_call, "args", {})
    if name != "execute":
        return None
    if isinstance(args, dict):
        return args.get("command", "") or ""
    return ""


def _is_indemn_mutating_call(cmd: str) -> bool:
    """True if `cmd` is an `indemn` CLI invocation with a non-read-only verb.

    Format: `indemn <entity> <verb> [...]`. Anything beyond list/get counts as
    a mutation — covers create, update, transition, capability invocations
    (auto-classify, fetch-new, etc.), and `skill update`.
    """
    parts = cmd.strip().split()
    if len(parts) < 3 or parts[0] != "indemn":
        return False
    verb = parts[2]
    return verb not in READ_ONLY_VERBS


def agent_did_useful_work(messages) -> tuple[bool, str | None]:
    """Did the agent produce meaningful state changes or output?

    Returns (True, None) if useful, or (False, reason) if not. The reason is
    suitable to pass as `--reason` to `indemn queue fail`.
    """
    last_ai_content = ""

    for msg in messages:
        msg_type = getattr(msg, "type", type(msg).__name__)
        if msg_type != "ai":
            continue

        last_ai_content = str(getattr(msg, "content", "") or "")

        for tc in getattr(msg, "tool_calls", []) or []:
            cmd = _extract_execute_command(tc)
            if cmd and _is_indemn_mutating_call(cmd):
                return (True, None)

    if last_ai_content.strip():
        return (True, None)

    return (False, NO_USEFUL_WORK_REASON)
