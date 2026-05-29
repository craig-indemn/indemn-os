"""Pin diagnose_actor's multi-source aggregation (CLI gap #10).

Pre-fix, `indemn diagnose actor <id>` only queried message_queue. That
silently missed completed runs (they're moved to message_log on
completion). Effectively: MC/TS/IE/Evaluator runs showed count=0 in
diagnose output because their work completes — moving to log — while the
failed/dead_letter case is rare.

Post-fix, diagnose_actor queries three sources:
  1. message_queue   — in-flight + failed + dead_letter + parked + pending
  2. message_log     — completed runs (the common case)
  3. traces          — LLM execution records for reasoning/hybrid associates

Merges by message_id; trace data layered onto matching message rows.

Source-shape pins (the endpoint is async + DB-heavy; full integration is
in test_diagnose_commands.py separately).
"""

from pathlib import Path

_DIAGNOSE_ROUTES = (
    Path(__file__).resolve().parents[2]
    / "kernel" / "api" / "diagnose_routes.py"
)


def _read_diagnose_source() -> str:
    return _DIAGNOSE_ROUTES.read_text()


def test_diagnose_actor_imports_message_log():
    """MessageLog is imported alongside Message."""
    src = _read_diagnose_source()
    assert "from kernel.message.schema import Message, MessageLog" in src


def test_diagnose_actor_queries_message_log():
    """message_log is queried for completed runs (the most common case)."""
    src = _read_diagnose_source()
    assert "log_msgs = await MessageLog.find(" in src, (
        "diagnose_actor must query MessageLog for completed runs"
    )


def test_diagnose_actor_queries_trace_collection():
    """Trace collection is queried for LLM execution records."""
    src = _read_diagnose_source()
    assert "from kernel_entities.trace import Trace" in src, (
        "Trace entity must be imported"
    )
    assert "traces = await Trace.find(" in src, (
        "diagnose_actor must query Trace for LLM run records"
    )


def test_diagnose_actor_queue_query_excludes_completed():
    """The message_queue branch should EXCLUDE 'completed' status — those
    live in message_log now (cold storage). Pre-fix the queue query
    included 'completed' which would never match anything in the queue
    collection."""
    src = _read_diagnose_source()
    # Get the queue_msgs query block
    start = src.index("queue_msgs = await Message.find(")
    end = src.index(".to_list()", start)
    block = src[start:end]
    # Must include failed/dead_letter
    assert '"failed"' in block
    assert '"dead_letter"' in block
    # Must NOT include "completed" — those are in MessageLog
    assert '"completed"' not in block, (
        "queue branch must not query for 'completed' — those moved to log"
    )


def test_diagnose_actor_merges_by_message_id():
    """Result rows are keyed by message_id, so a single run with both a
    message_log entry AND a trace appears as ONE row with merged fields."""
    src = _read_diagnose_source()
    assert "by_key" in src and "_row(" in src, (
        "Result merging by message_id required"
    )
    # Trace fields layered onto matching message rows
    assert '"trace_id": str(tr.id)' in src
    assert '"langsmith_run_id"' in src
    assert '"execution_status"' in src
    assert '"total_tokens"' in src


def test_diagnose_actor_response_includes_sources_breakdown():
    """The response includes a `sources` block showing per-source counts —
    so an operator can see at-a-glance whether queue/log/traces all
    returned data, or if one source is empty (a signal in itself)."""
    src = _read_diagnose_source()
    assert '"sources":' in src, (
        "Response must include sources breakdown for transparency"
    )
    assert '"message_queue":' in src
    assert '"message_log":' in src
    assert '"traces":' in src


def test_diagnose_actor_handles_trace_only_runs():
    """A Trace might exist without a corresponding message row (edge case:
    trace creation succeeded but the message row was cleaned up, or some
    other path). Those rows should still surface with `source: trace_only`
    so they're not silently dropped."""
    src = _read_diagnose_source()
    assert '"trace_only"' in src, (
        "Orphan traces (no matching message) must surface with source=trace_only"
    )


def test_diagnose_actor_limits_each_source_query():
    """Each source query bounded by --limit so a single noisy actor can't
    fan out into a million-row scan."""
    src = _read_diagnose_source()
    # All three source queries should have .limit(limit) chained
    queue_block = src[src.index("queue_msgs ="):src.index("queue_msgs = ", src.index("queue_msgs =") + 1) if "queue_msgs = " in src[src.index("queue_msgs =") + 1:] else src.index(".to_list()", src.index("queue_msgs ="))]
    log_block = src[src.index("log_msgs ="):src.index(".to_list()", src.index("log_msgs ="))]
    traces_block = src[src.index("traces = await Trace"):src.index(".to_list()", src.index("traces = await Trace"))]
    assert ".limit(limit)" in queue_block, "queue query missing limit"
    assert ".limit(limit)" in log_block, "log query missing limit"
    assert ".limit(limit)" in traces_block, "traces query missing limit"
