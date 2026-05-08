"""Trace creation helpers — message serialization, run tree capture, token aggregation.

Produces clean, LangSmith-aligned trace data:
- Messages: clean step-by-step execution record (stripped of LangChain internals)
- Child_runs: the actual LangSmith run tree captured via collect_runs()
- Tokens: aggregated from AIMessage usage_metadata
"""

import logging

log = logging.getLogger(__name__)


def serialize_run_tree(run) -> list[dict]:
    """Serialize a LangChain RunTree into a list of child run dicts.

    Captures the same execution tree that LangSmith stores. Each node
    has: id, name, run_type, inputs, outputs, child_runs (recursive),
    error, start_time, end_time.

    Filters out middleware internals (TodoListMiddleware, etc.) to keep
    only the meaningful execution steps: model calls, tool calls, and
    the top-level chain.
    """
    if not run:
        return []

    def _serialize_node(node, depth=0) -> dict | None:
        name = getattr(node, "name", "unknown")
        run_type = getattr(node, "run_type", "chain")

        # Skip middleware wrappers — they add noise without evaluation value
        _MIDDLEWARE_PREFIXES = (
            "TodoListMiddleware",
            "AnthropicPromptCachingMiddleware",
            "SummarizationMiddleware",
            "SubAgentMiddleware",
            "FilesystemMiddleware",
            "ExecuteErrorStatusMiddleware",
            "PatchToolCallsMiddleware",
        )
        if any(name.startswith(prefix) for prefix in _MIDDLEWARE_PREFIXES):
            return None

        inputs_raw = getattr(node, "inputs", {}) or {}
        outputs_raw = getattr(node, "outputs", {}) or {}

        # Clean inputs/outputs — truncate large values for storage
        def _clean_dict(d, max_val_len=5000):
            if not isinstance(d, dict):
                return d
            cleaned = {}
            for k, v in d.items():
                if isinstance(v, str) and len(v) > max_val_len:
                    cleaned[k] = v[:max_val_len] + f"... [{len(v)} chars total]"
                elif isinstance(v, list) and len(str(v)) > max_val_len:
                    cleaned[k] = f"[list with {len(v)} items, {len(str(v))} chars total]"
                else:
                    cleaned[k] = v
            return cleaned

        serialized_children = []
        for child in getattr(node, "child_runs", []) or []:
            child_dict = _serialize_node(child, depth + 1)
            if child_dict:
                serialized_children.append(child_dict)

        start = getattr(node, "start_time", None)
        end = getattr(node, "end_time", None)

        result = {
            "id": str(getattr(node, "id", "")),
            "name": name,
            "run_type": run_type,
            "inputs": _clean_dict(inputs_raw),
            "outputs": _clean_dict(outputs_raw),
            "child_runs": serialized_children,
            "error": getattr(node, "error", None),
        }
        if start:
            result["start_time"] = start.isoformat() if hasattr(start, "isoformat") else str(start)
        if end:
            result["end_time"] = end.isoformat() if hasattr(end, "isoformat") else str(end)

        return result

    # Serialize root's children (skip the root itself — it's the top-level chain)
    children = []
    for child in getattr(run, "child_runs", []) or []:
        child_dict = _serialize_node(child)
        if child_dict:
            children.append(child_dict)

    return children


def _clean_message(msg) -> dict:
    """Extract only the evaluation-relevant fields from a LangChain message.

    Strips internal metadata (additional_kwargs, response_metadata,
    usage_metadata) that bloats the trace without helping evaluation.
    Keeps: type, content, tool_calls (cleaned), tool_call_id, name, status.
    """
    if isinstance(msg, dict):
        mtype = msg.get("type", "unknown")
        result = {"type": mtype}
        if msg.get("content"):
            result["content"] = msg["content"]
        if msg.get("tool_calls"):
            result["tool_calls"] = [
                {"name": tc.get("name", ""), "args": tc.get("args", {}), "id": tc.get("id", "")}
                for tc in msg["tool_calls"]
            ]
        if msg.get("tool_call_id"):
            result["tool_call_id"] = msg["tool_call_id"]
        if msg.get("name"):
            result["name"] = msg["name"]
        if msg.get("status") and msg["status"] != "success":
            result["status"] = msg["status"]
        return result

    mtype = getattr(msg, "type", "unknown")
    result = {"type": mtype}

    content = getattr(msg, "content", "")
    if content:
        result["content"] = str(content)

    tool_calls = getattr(msg, "tool_calls", [])
    if tool_calls:
        result["tool_calls"] = [
            {
                "name": tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", ""),
                "args": tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {}),
                "id": tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", ""),
            }
            for tc in tool_calls
        ]

    tool_call_id = getattr(msg, "tool_call_id", "")
    if tool_call_id:
        result["tool_call_id"] = tool_call_id

    name = getattr(msg, "name", "")
    if name:
        result["name"] = name

    status = getattr(msg, "status", "success")
    if status != "success":
        result["status"] = status

    return result


def serialize_messages(messages: list) -> list[dict]:
    """Serialize LangChain messages to clean dicts for Trace storage.

    Strips internal LangChain metadata (additional_kwargs, response_metadata,
    usage_metadata). Keeps only what matters for evaluation: the conversation
    flow (type, content, tool_calls, tool results).
    """
    return [_clean_message(msg) for msg in messages]


def derive_child_runs(messages: list) -> list[dict]:
    """Build execution step list from flat message list.

    Each step represents one agent action: the LLM decided to call a tool,
    here are the args, here's what came back. Matches the LangSmith Run
    tree node shape from spec §6.1.

    For each AIMessage→ToolMessage pair, produces a node with:
    - id, name, run_type, inputs, outputs, error
    """
    child_runs = []
    pending_calls: dict = {}

    for msg in messages:
        msg_type = getattr(msg, "type", "") if not isinstance(msg, dict) else msg.get("type", "")

        if msg_type == "ai":
            tool_calls = getattr(msg, "tool_calls", []) if not isinstance(msg, dict) else msg.get("tool_calls", [])
            for tc in tool_calls:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                tc_args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
                if tc_id:
                    pending_calls[tc_id] = {"name": tc_name, "args": tc_args}

        elif msg_type == "tool":
            if isinstance(msg, dict):
                tc_id = msg.get("tool_call_id")
                output = str(msg.get("content", ""))
                status = msg.get("status", "success")
            else:
                tc_id = getattr(msg, "tool_call_id", None)
                output = str(getattr(msg, "content", ""))
                status = getattr(msg, "status", "success")

            if tc_id and tc_id in pending_calls:
                call = pending_calls.pop(tc_id)
                child_runs.append({
                    "id": tc_id,
                    "name": call["name"],
                    "run_type": "tool",
                    "inputs": call["args"],
                    "outputs": output[:10000],
                    "child_runs": [],
                    "error": output[:2000] if status != "success" else None,
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
