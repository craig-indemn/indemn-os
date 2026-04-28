"""Tests for completion_logic.agent_did_useful_work.

Tightened contract (Apr 28 follow-up to Bug #2): useful work requires
≥1 *successful* mutating `indemn` CLI call. Read-only calls (list/get),
attempted-but-failed mutating calls, and non-empty narrative content alone
do NOT count as useful work — that pattern surfaced today on the Diana@CKSpecialty
EC trace, where the agent attempted `email create` twice (both failed with
shell escape + E11000) then concluded "the email already exists" — Diana stayed
at status=received, no transition, no link, but the message was marked complete.

Domain-agnostic by design: the harness still doesn't know what each associate
is supposed to do. It only observes "did any state-changing call succeed?".
Deeper detection of "agent didn't fulfill skill intent" lives in evals
(Phase E) and observability on top of LangSmith — not in the harness.
"""

from types import SimpleNamespace

from completion_logic import agent_did_useful_work

# --- Test helpers ---

# Real LangChain wraps every command output with one of these markers.
SUCCESS = "[Command succeeded with exit code 0]"
FAILURE = "[Command failed with exit code 2]"


def _ai(command: str, tool_call_id: str = "tc_1", tool_name: str = "execute"):
    """AI message with a single tool call. tool_call_id matches the linked tool result."""
    return SimpleNamespace(
        type="ai",
        content="",
        tool_calls=[{"name": tool_name, "args": {"command": command}, "id": tool_call_id}],
    )


def _ai_no_tools(content: str = ""):
    """AI message with no tool calls (often the final 'thinking' message)."""
    return SimpleNamespace(type="ai", content=content, tool_calls=[])


def _tool_ok(content: str, tool_call_id: str = "tc_1"):
    """Tool result message — successful execution."""
    body = f"{content}\n\n{SUCCESS}" if SUCCESS not in content else content
    return SimpleNamespace(type="tool", name="execute", content=body, tool_call_id=tool_call_id)


def _tool_fail(stderr_content: str, tool_call_id: str = "tc_1"):
    """Tool result message — failed execution (stderr + failure marker)."""
    return SimpleNamespace(
        type="tool",
        name="execute",
        content=f"[stderr] {stderr_content}\n\n{FAILURE}",
        tool_call_id=tool_call_id,
    )


# --- Fail cases ---


def test_empty_messages_returns_false():
    did, reason = agent_did_useful_work([])
    assert did is False
    assert reason


def test_only_thinking_no_tools_returns_false():
    did, _ = agent_did_useful_work([_ai_no_tools()])
    assert did is False


def test_only_indemn_list_returns_false():
    """Read-only `indemn ... list` doesn't count, even when it succeeds."""
    msgs = [
        _ai("indemn company list --limit 50"),
        _tool_ok("[ ... 50 records ... ]"),
        _ai_no_tools(),
    ]
    did, _ = agent_did_useful_work(msgs)
    assert did is False


def test_only_indemn_get_returns_false():
    msgs = [
        _ai("indemn touchpoint get 69eb"),
        _tool_ok("{ ... touchpoint dump ... }"),
        _ai_no_tools(),
    ]
    did, _ = agent_did_useful_work(msgs)
    assert did is False


def test_only_skill_get_returns_false():
    """`indemn skill get X` is read-only docs lookup."""
    msgs = [
        _ai("indemn skill get Touchpoint"),
        _tool_ok("# Touchpoint\n## Fields\n..."),
        _ai_no_tools(),
    ]
    did, _ = agent_did_useful_work(msgs)
    assert did is False


def test_only_grep_and_read_file_returns_false():
    """Non-indemn tools never count as state changes (GR Little case)."""
    msgs = [
        _ai("/large_tool_results/abc...", tool_name="read_file"),
        SimpleNamespace(type="tool", name="read_file", content="...", tool_call_id="tc_1"),
        _ai("INDEM /large_tool_results/abc...", tool_call_id="tc_2", tool_name="grep"),
        SimpleNamespace(type="tool", name="grep", content="no match", tool_call_id="tc_2"),
        _ai_no_tools(),
    ]
    did, _ = agent_did_useful_work(msgs)
    assert did is False


def test_failed_mutating_call_returns_false():
    """Today's Diana@CKSpecialty case: agent attempts `email create`, fails with E11000.

    Without this check the harness used to mark the message complete because
    "tool_calls included a mutating verb" — even though the verb didn't succeed.
    """
    msgs = [
        _ai("indemn skill get Email"),
        _tool_ok("# Email\n..."),
        _ai("indemn skill get Company", tool_call_id="tc_2"),
        _tool_ok("# Company\n...", tool_call_id="tc_2"),
        _ai(
            'indemn company entity-resolve --data \'{"candidate":{"domain":"x.com"}}\'',
            tool_call_id="tc_3",
        ),
        _tool_ok('{"candidates":[{"_id":"...","score":1.0}]}', tool_call_id="tc_3"),
        _ai('indemn email create --data \'{...}\'', tool_call_id="tc_4"),
        _tool_fail('/bin/sh: 9: Syntax error: "(" unexpected', tool_call_id="tc_4"),
        _ai('indemn email create --data \'{...}\'', tool_call_id="tc_5"),
        _tool_fail("Error 500: E11000 duplicate key error", tool_call_id="tc_5"),
        _ai_no_tools("The email already exists in the system."),
    ]
    did, reason = agent_did_useful_work(msgs)
    assert did is False, f"Expected False, got True. Reason: {reason}"
    assert reason and "successful" in reason.lower()


def test_meaningful_final_content_alone_returns_false():
    """Removed in Apr 28 tightening: narrative content is not a substitute for
    actually mutating an entity. Agent that only reads + narrates is stuck."""
    msgs = [
        _ai("indemn touchpoint get 69eb..."),
        _tool_ok("{...}"),
        _ai_no_tools(
            "I reviewed the touchpoint. Nothing actionable — this is a "
            "scheduling-only email."
        ),
    ]
    did, _ = agent_did_useful_work(msgs)
    assert did is False


def test_failed_mutation_with_no_tool_call_id_match_returns_false():
    """Stale or missing tool_call_id linkage doesn't accidentally pass."""
    msgs = [
        _ai("indemn email update 69eb... --data '{}'", tool_call_id="tc_1"),
        # Tool message with a different ID — doesn't link
        _tool_ok("ok", tool_call_id="tc_99"),
        _ai_no_tools(),
    ]
    did, _ = agent_did_useful_work(msgs)
    assert did is False


# --- Success cases ---


def test_indemn_create_succeeds_returns_true():
    msgs = [
        _ai('indemn task create --data \'{"title":"Send recap"}\''),
        _tool_ok('{"_id": "69ec..."}'),
        _ai_no_tools(),
    ]
    did, reason = agent_did_useful_work(msgs)
    assert did is True
    assert reason is None


def test_indemn_update_succeeds_returns_true():
    msgs = [
        _ai('indemn email update 69eb... --data \'{"company":"69ec..."}\''),
        _tool_ok('{"_id":"69eb...","company":"69ec..."}'),
        _ai_no_tools(),
    ]
    did, _ = agent_did_useful_work(msgs)
    assert did is True


def test_indemn_transition_succeeds_returns_true():
    msgs = [
        _ai("indemn touchpoint transition 69eb... --to processed"),
        _tool_ok('{"_id":"69eb...","status":"processed"}'),
        _ai_no_tools(),
    ]
    did, _ = agent_did_useful_work(msgs)
    assert did is True


def test_indemn_capability_invocation_succeeds_returns_true():
    msgs = [
        _ai("indemn email auto-classify 69eb... --auto"),
        _tool_ok('{"needs_reasoning": false, "result": {...}}'),
        _ai_no_tools(),
    ]
    did, _ = agent_did_useful_work(msgs)
    assert did is True


def test_indemn_fetch_new_succeeds_returns_true():
    msgs = [
        _ai('indemn meeting fetch-new --data \'{"since":"2026-04-22"}\''),
        _tool_ok('{"fetched": 12, "created": 12}'),
        _ai_no_tools(),
    ]
    did, _ = agent_did_useful_work(msgs)
    assert did is True


def test_mixed_read_and_successful_write_returns_true():
    """Agent does read-only exploration + a single successful mutation."""
    msgs = [
        _ai("indemn touchpoint get 69eb..."),
        _tool_ok("{...}"),
        _ai("indemn meeting get 69ed...", tool_call_id="tc_2"),
        _tool_ok("{...transcript...}", tool_call_id="tc_2"),
        _ai('indemn signal create --data \'{"description":"Walker is champion"}\'', tool_call_id="tc_3"),
        _tool_ok('{"_id":"69ee..."}', tool_call_id="tc_3"),
        _ai_no_tools(),
    ]
    did, _ = agent_did_useful_work(msgs)
    assert did is True


def test_failed_then_succeeded_returns_true():
    """Agent's first attempt failed, retry succeeded — that still counts."""
    msgs = [
        _ai('indemn email update 69eb... --data \'{"bad":"shape"}\'', tool_call_id="tc_1"),
        _tool_fail("Error 422: bad shape", tool_call_id="tc_1"),
        _ai('indemn email update 69eb... --data \'{"company":"69ec..."}\'', tool_call_id="tc_2"),
        _tool_ok('{"_id":"69eb..."}', tool_call_id="tc_2"),
        _ai_no_tools(),
    ]
    did, _ = agent_did_useful_work(msgs)
    assert did is True


# --- Format compatibility: tool_calls as objects vs dicts ---


def test_tool_call_as_object_returns_true():
    """LangChain sometimes serializes tool_calls as objects with .name/.args/.id attrs."""
    tc_obj = SimpleNamespace(
        name="execute",
        args={"command": 'indemn task create --data \'{"title":"x"}\''},
        id="tc_1",
    )
    msgs = [
        SimpleNamespace(type="ai", content="", tool_calls=[tc_obj]),
        _tool_ok('{"_id":"69ec..."}'),
        _ai_no_tools(),
    ]
    did, _ = agent_did_useful_work(msgs)
    assert did is True


def test_tool_call_as_dict_returns_true():
    msgs = [
        _ai('indemn task create --data \'{"title":"x"}\''),
        _tool_ok('{"_id":"69ec..."}'),
        _ai_no_tools(),
    ]
    did, _ = agent_did_useful_work(msgs)
    assert did is True


# --- Edge cases ---


def test_human_messages_ignored():
    msgs = [
        SimpleNamespace(type="human", content="Process this work: ..."),
        _ai_no_tools(),
    ]
    did, _ = agent_did_useful_work(msgs)
    assert did is False


def test_non_execute_tool_call_ignored():
    """write_file / grep / etc. don't mutate OS entities."""
    msgs = [
        SimpleNamespace(
            type="ai",
            content="",
            tool_calls=[{"name": "write_file", "args": {"path": "/tmp/foo", "content": "x"}, "id": "tc_1"}],
        ),
        SimpleNamespace(type="tool", name="write_file", content="ok", tool_call_id="tc_1"),
        _ai_no_tools(),
    ]
    did, _ = agent_did_useful_work(msgs)
    assert did is False


def test_indemn_call_too_short_ignored():
    msgs = [
        _ai("indemn"),
        _tool_ok("usage: ..."),
        _ai("indemn --help", tool_call_id="tc_2"),
        _tool_ok("usage: ...", tool_call_id="tc_2"),
        _ai_no_tools(),
    ]
    did, _ = agent_did_useful_work(msgs)
    assert did is False


def test_command_with_pipes_recognizes_indemn_at_start():
    msgs = [
        _ai('indemn task create --data \'{"title":"x"}\' | jq \'._id\''),
        _tool_ok('"69ec..."'),
        _ai_no_tools(),
    ]
    did, _ = agent_did_useful_work(msgs)
    assert did is True


def test_command_with_leading_whitespace_handled():
    msgs = [
        _ai("   indemn task create --data '{}'"),
        _tool_ok('{"_id":"69ec..."}'),
        _ai_no_tools(),
    ]
    did, _ = agent_did_useful_work(msgs)
    assert did is True


def test_stderr_alone_marks_failure():
    """Even without the `[Command failed]` marker, [stderr] in output = failure."""
    msgs = [
        _ai('indemn email update 69eb... --data \'{}\''),
        SimpleNamespace(
            type="tool",
            name="execute",
            content="[stderr] some warning\n[some output]",
            tool_call_id="tc_1",
        ),
        _ai_no_tools(),
    ]
    did, _ = agent_did_useful_work(msgs)
    assert did is False
