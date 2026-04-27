"""Tests for completion_logic.agent_did_useful_work.

Bug #2 (silent workflow stuck-state): the harness was unconditionally calling
`indemn queue complete` after agent.ainvoke(), even when the agent produced no
output and made no mutating CLI calls. Messages then sat in `processing`
indefinitely with `last_error: null` (Apr 24 GR Little Intelligence Extractor
trace).

The fix: detect "the agent did nothing useful" and call `indemn queue fail`
with a clear reason instead. This module's `agent_did_useful_work(messages)`
is the detection function.

"Useful work" = at least one of:
  (a) at least one mutating `indemn` CLI call (anything but list/get/stats/health),
  (b) the final AI message has non-empty content (an actual response).
"""

from types import SimpleNamespace

import pytest

from completion_logic import agent_did_useful_work


def _ai_msg_with_tool_call(command: str, tool_name: str = "execute"):
    """Build an AI message with a single tool call to `tool_name` running `command`."""
    return SimpleNamespace(
        type="ai",
        content="",
        tool_calls=[{"name": tool_name, "args": {"command": command}}],
    )


def _ai_msg_with_content(content: str):
    """Build an AI message with non-empty content and no tool calls."""
    return SimpleNamespace(type="ai", content=content, tool_calls=[])


def _ai_msg_thinking_no_content_no_tools():
    """Build an AI message with empty content and no tool calls (a 'stuck' final message)."""
    return SimpleNamespace(type="ai", content="", tool_calls=[])


def _tool_msg(name: str, content: str = ""):
    """Build a tool result message (not an AI message — these don't have tool_calls)."""
    return SimpleNamespace(type="tool", name=name, content=content)


# --- Fail cases: agent did nothing useful ---


def test_empty_messages_returns_false():
    did, reason = agent_did_useful_work([])
    assert did is False
    assert reason and len(reason) > 0


def test_only_thinking_no_tools_returns_false():
    """Agent had a single AI message with empty content and no tool calls."""
    messages = [_ai_msg_thinking_no_content_no_tools()]
    did, reason = agent_did_useful_work(messages)
    assert did is False
    assert reason and "without producing" in reason.lower()


def test_only_indemn_list_returns_false():
    """Agent only ran read-only `indemn ... list` calls."""
    messages = [
        _ai_msg_with_tool_call("indemn company list --limit 50"),
        _tool_msg("execute", "[ ... 50 records ... ]"),
        _ai_msg_thinking_no_content_no_tools(),
    ]
    did, reason = agent_did_useful_work(messages)
    assert did is False, f"Expected False, got True. Reason: {reason}"


def test_only_indemn_get_returns_false():
    """Agent only ran read-only `indemn ... get` calls."""
    messages = [
        _ai_msg_with_tool_call("indemn touchpoint get 69eb"),
        _tool_msg("execute", "{ ... touchpoint dump ... }"),
        _ai_msg_thinking_no_content_no_tools(),
    ]
    did, reason = agent_did_useful_work(messages)
    assert did is False


def test_only_skill_get_returns_false():
    """`indemn skill get X` is read-only — agent reading docs, not doing work."""
    messages = [
        _ai_msg_with_tool_call("indemn skill get Touchpoint"),
        _tool_msg("execute", "# Touchpoint\n## Fields\n..."),
        _ai_msg_thinking_no_content_no_tools(),
    ]
    did, reason = agent_did_useful_work(messages)
    assert did is False


def test_grep_and_read_file_only_returns_false():
    """Agent only used non-indemn tools (grep, read_file) — exactly the GR Little case."""
    messages = [
        _ai_msg_with_tool_call("/large_tool_results/abc...", tool_name="read_file"),
        _tool_msg("read_file", "..."),
        _ai_msg_with_tool_call("INDEM /large_tool_results/abc...", tool_name="grep"),
        _tool_msg("grep", "no match"),
        _ai_msg_thinking_no_content_no_tools(),
    ]
    did, reason = agent_did_useful_work(messages)
    assert did is False, f"Expected False, got True. Reason: {reason}"


# --- Success cases: agent did real work ---


def test_indemn_create_returns_true():
    """A single `indemn ... create` call is enough to mark complete."""
    messages = [
        _ai_msg_with_tool_call(
            'indemn task create --data \'{"title":"Send recap","company":"69eb..."}\''
        ),
        _tool_msg("execute", '{"_id": "69ec..."}'),
        _ai_msg_thinking_no_content_no_tools(),
    ]
    did, reason = agent_did_useful_work(messages)
    assert did is True
    assert reason is None


def test_indemn_update_returns_true():
    messages = [
        _ai_msg_with_tool_call('indemn email update 69eb... --data \'{"company":"69ec..."}\''),
        _tool_msg("execute", "ok"),
        _ai_msg_thinking_no_content_no_tools(),
    ]
    did, reason = agent_did_useful_work(messages)
    assert did is True


def test_indemn_transition_returns_true():
    messages = [
        _ai_msg_with_tool_call("indemn touchpoint transition 69eb... --to processed"),
        _tool_msg("execute", "ok"),
        _ai_msg_thinking_no_content_no_tools(),
    ]
    did, reason = agent_did_useful_work(messages)
    assert did is True


def test_indemn_capability_invocation_returns_true():
    """A capability invocation like `auto-classify` is a mutation (sets fields + saves)."""
    messages = [
        _ai_msg_with_tool_call("indemn email auto-classify 69eb... --auto"),
        _tool_msg("execute", '{"needs_reasoning": false, "result": {...}}'),
        _ai_msg_thinking_no_content_no_tools(),
    ]
    did, reason = agent_did_useful_work(messages)
    assert did is True


def test_indemn_fetch_new_returns_true():
    """Collection-level capability `fetch-new` creates entities."""
    messages = [
        _ai_msg_with_tool_call('indemn meeting fetch-new --data \'{"since":"2026-04-22"}\''),
        _tool_msg("execute", '{"fetched": 12, "created": 12}'),
        _ai_msg_thinking_no_content_no_tools(),
    ]
    did, reason = agent_did_useful_work(messages)
    assert did is True


def test_meaningful_final_content_returns_true():
    """Agent gave a real final response — not a silent stuck state."""
    messages = [
        _ai_msg_with_tool_call("indemn touchpoint get 69eb..."),
        _tool_msg("execute", "{...}"),
        _ai_msg_with_content(
            "I reviewed the touchpoint. Nothing actionable — this is a scheduling-only "
            "email. No tasks, decisions, or signals to extract."
        ),
    ]
    did, reason = agent_did_useful_work(messages)
    assert did is True


def test_mixed_read_and_write_returns_true():
    """Agent does read-only exploration + a single write — still counts as useful."""
    messages = [
        _ai_msg_with_tool_call("indemn touchpoint get 69eb..."),
        _tool_msg("execute", "{...}"),
        _ai_msg_with_tool_call("indemn meeting get 69ed..."),
        _tool_msg("execute", "{...transcript...}"),
        _ai_msg_with_tool_call(
            'indemn signal create --data \'{"description":"Walker is champion"}\''
        ),
        _tool_msg("execute", "ok"),
        _ai_msg_thinking_no_content_no_tools(),
    ]
    did, reason = agent_did_useful_work(messages)
    assert did is True


# --- Format compatibility: tool_calls as objects vs dicts ---


def test_tool_call_as_object_returns_true():
    """LangChain sometimes serializes tool_calls as objects with .name/.args attrs."""
    tc_object = SimpleNamespace(
        name="execute",
        args={"command": 'indemn task create --data \'{"title":"x"}\''},
    )
    msg = SimpleNamespace(type="ai", content="", tool_calls=[tc_object])
    messages = [msg, _ai_msg_thinking_no_content_no_tools()]
    did, reason = agent_did_useful_work(messages)
    assert did is True


def test_tool_call_as_dict_returns_true():
    """LangChain also serializes tool_calls as plain dicts."""
    messages = [
        _ai_msg_with_tool_call('indemn task create --data \'{"title":"x"}\''),
        _ai_msg_thinking_no_content_no_tools(),
    ]
    did, reason = agent_did_useful_work(messages)
    assert did is True


# --- Edge cases ---


def test_human_messages_ignored():
    """Human messages don't count as agent work."""
    messages = [
        SimpleNamespace(type="human", content="Process this work: ..."),
        _ai_msg_thinking_no_content_no_tools(),
    ]
    did, reason = agent_did_useful_work(messages)
    assert did is False


def test_non_execute_tool_call_ignored():
    """Tool calls to other tools (write_file, etc.) without indemn don't count as mutations."""
    messages = [
        SimpleNamespace(
            type="ai",
            content="",
            tool_calls=[{"name": "write_file", "args": {"path": "/tmp/foo", "content": "x"}}],
        ),
        _ai_msg_thinking_no_content_no_tools(),
    ]
    did, reason = agent_did_useful_work(messages)
    # write_file isn't a mutation of an OS entity — caller would still mark this stuck.
    assert did is False


def test_indemn_call_without_entity_verb_ignored():
    """Malformed `indemn` calls (too short) don't crash and don't count."""
    messages = [
        _ai_msg_with_tool_call("indemn"),  # no args
        _ai_msg_with_tool_call("indemn --help"),
        _ai_msg_thinking_no_content_no_tools(),
    ]
    did, reason = agent_did_useful_work(messages)
    assert did is False


def test_command_with_pipes_handled():
    """Commands with shell pipes still recognize the indemn invocation at the start."""
    messages = [
        _ai_msg_with_tool_call(
            "indemn task create --data '{\"title\":\"x\"}' | jq '._id'"
        ),
        _ai_msg_thinking_no_content_no_tools(),
    ]
    did, reason = agent_did_useful_work(messages)
    assert did is True


def test_command_with_leading_whitespace_handled():
    """Commands with leading whitespace are still parsed."""
    messages = [
        _ai_msg_with_tool_call("   indemn task create --data '{}'"),
        _ai_msg_thinking_no_content_no_tools(),
    ]
    did, reason = agent_did_useful_work(messages)
    assert did is True
