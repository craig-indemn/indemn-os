"""Trace creation helpers — message serialization, child_runs derivation, token aggregation.

Extracted from main.py for testability (main.py has heavy imports
that can't load in the test environment).
"""

import json
import logging

log = logging.getLogger(__name__)


def serialize_messages(messages: list) -> list[dict]:
    """Serialize LangChain message objects to dicts for Trace storage."""
    serialized = []
    for msg in messages:
        if hasattr(msg, "model_dump"):
            serialized.append(msg.model_dump())
        elif isinstance(msg, dict):
            serialized.append(msg)
        else:
            serialized.append({
                "type": getattr(msg, "type", "unknown"),
                "content": str(getattr(msg, "content", "")),
            })
    return serialized


def derive_child_runs(messages: list) -> list[dict]:
    """Build tool call tree from flat message list.

    Pairs AIMessage.tool_calls with corresponding ToolMessage results.
    Node shape per evaluation framework spec §6.1.
    """
    child_runs = []
    pending_calls: dict = {}
    for msg in messages:
        msg_type = getattr(msg, "type", "")
        if msg_type == "ai":
            for tc in getattr(msg, "tool_calls", []):
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                tc_args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
                if tc_id:
                    pending_calls[tc_id] = {"name": tc_name, "args": tc_args}
        elif msg_type == "tool":
            tc_id = getattr(msg, "tool_call_id", None)
            if tc_id and tc_id in pending_calls:
                call = pending_calls.pop(tc_id)
                output = str(getattr(msg, "content", ""))
                status = getattr(msg, "status", "success")
                child_runs.append({
                    "id": tc_id,
                    "name": call["name"],
                    "run_type": "tool",
                    "inputs": call["args"],
                    "outputs": output[:10000],
                    "child_runs": [],
                    "error": output if status != "success" else None,
                    "tokens": {},
                    "timing": {},
                })
    return child_runs


def aggregate_tokens(messages: list) -> tuple[int, int, int]:
    """Sum token usage across all AIMessages."""
    prompt_tokens = completion_tokens = total_tokens = 0
    for msg in messages:
        usage = getattr(msg, "usage_metadata", None)
        if usage and isinstance(usage, dict):
            prompt_tokens += usage.get("input_tokens", 0)
            completion_tokens += usage.get("output_tokens", 0)
            total_tokens += usage.get("total_tokens", 0)
    return prompt_tokens, completion_tokens, total_tokens
