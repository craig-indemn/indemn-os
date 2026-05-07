"""Tests for trace creation helpers — serialization, child_runs, token aggregation."""

import json
from types import SimpleNamespace

from trace_helpers import serialize_messages, derive_child_runs, aggregate_tokens


def _ai_msg(content="", tool_calls=None, usage_metadata=None):
    msg = SimpleNamespace(type="ai", content=content, tool_calls=tool_calls or [])
    if usage_metadata:
        msg.usage_metadata = usage_metadata
    return msg


def _tool_msg(content="", tool_call_id="tc_1", name="execute", status="success"):
    return SimpleNamespace(
        type="tool", content=content, tool_call_id=tool_call_id,
        name=name, status=status,
    )


def _human_msg(content="Process this work"):
    return SimpleNamespace(type="human", content=content)


class TestSerializeMessages:
    def test_dict_passthrough(self):
        msg = {"role": "user", "content": "hello"}
        result = serialize_messages([msg])
        assert result == [msg]

    def test_model_dump(self):
        class FakeMsg:
            type = "ai"
            content = "response"
            def model_dump(self):
                return {"type": "ai", "content": "response", "id": "123"}
        result = serialize_messages([FakeMsg()])
        assert result[0]["type"] == "ai"
        assert result[0]["content"] == "response"

    def test_raw_object_fallback(self):
        msg = SimpleNamespace(type="human", content="hello")
        result = serialize_messages([msg])
        assert result[0]["type"] == "human"
        assert result[0]["content"] == "hello"

    def test_mixed_types(self):
        messages = [
            {"role": "user", "content": "hi"},
            SimpleNamespace(type="ai", content="response"),
        ]
        result = serialize_messages(messages)
        assert len(result) == 2
        assert isinstance(result[0], dict)
        assert isinstance(result[1], dict)


class TestDeriveChildRuns:
    def test_pairs_tool_calls_with_results(self):
        messages = [
            _ai_msg(tool_calls=[{"id": "tc_1", "name": "execute", "args": {"cmd": "ls"}}]),
            _tool_msg(content="file1.txt\nfile2.txt", tool_call_id="tc_1"),
        ]
        runs = derive_child_runs(messages)
        assert len(runs) == 1
        assert runs[0]["id"] == "tc_1"
        assert runs[0]["name"] == "execute"
        assert runs[0]["run_type"] == "tool"
        assert runs[0]["inputs"] == {"cmd": "ls"}
        assert "file1.txt" in runs[0]["outputs"]
        assert runs[0]["child_runs"] == []
        assert runs[0]["error"] is None

    def test_error_tool_result(self):
        messages = [
            _ai_msg(tool_calls=[{"id": "tc_1", "name": "execute", "args": {}}]),
            _tool_msg(content="command not found", tool_call_id="tc_1", status="error"),
        ]
        runs = derive_child_runs(messages)
        assert runs[0]["error"] == "command not found"

    def test_multiple_tool_calls(self):
        messages = [
            _ai_msg(tool_calls=[
                {"id": "tc_1", "name": "execute", "args": {"cmd": "a"}},
                {"id": "tc_2", "name": "execute", "args": {"cmd": "b"}},
            ]),
            _tool_msg(content="result_a", tool_call_id="tc_1"),
            _tool_msg(content="result_b", tool_call_id="tc_2"),
        ]
        runs = derive_child_runs(messages)
        assert len(runs) == 2
        assert runs[0]["inputs"] == {"cmd": "a"}
        assert runs[1]["inputs"] == {"cmd": "b"}

    def test_unmatched_tool_call_skipped(self):
        messages = [
            _ai_msg(tool_calls=[{"id": "tc_1", "name": "execute", "args": {}}]),
        ]
        runs = derive_child_runs(messages)
        assert len(runs) == 0

    def test_output_truncated_at_10000(self):
        long_output = "x" * 15000
        messages = [
            _ai_msg(tool_calls=[{"id": "tc_1", "name": "execute", "args": {}}]),
            _tool_msg(content=long_output, tool_call_id="tc_1"),
        ]
        runs = derive_child_runs(messages)
        assert len(runs[0]["outputs"]) == 10000

    def test_spec_node_shape(self):
        messages = [
            _ai_msg(tool_calls=[{"id": "tc_1", "name": "execute", "args": {"cmd": "ls"}}]),
            _tool_msg(content="ok", tool_call_id="tc_1"),
        ]
        runs = derive_child_runs(messages)
        node = runs[0]
        assert set(node.keys()) == {"id", "name", "run_type", "inputs", "outputs", "child_runs", "error", "tokens", "timing"}


class TestAggregateTokens:
    def test_sums_usage_metadata(self):
        messages = [
            _ai_msg(usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}),
            _ai_msg(usage_metadata={"input_tokens": 200, "output_tokens": 100, "total_tokens": 300}),
        ]
        p, c, t = aggregate_tokens(messages)
        assert p == 300
        assert c == 150
        assert t == 450

    def test_handles_missing_usage(self):
        messages = [
            _ai_msg(),
            _human_msg(),
            _tool_msg(),
        ]
        p, c, t = aggregate_tokens(messages)
        assert p == 0
        assert c == 0
        assert t == 0

    def test_mixed_with_and_without_usage(self):
        messages = [
            _ai_msg(usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}),
            _ai_msg(),
            _tool_msg(),
        ]
        p, c, t = aggregate_tokens(messages)
        assert p == 100
        assert c == 50
        assert t == 150
