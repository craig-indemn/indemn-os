"""Async harness entry point.

Subscribes to task queue `runtime-{runtime_id}` (G1.1).
Registers `process_with_associate` activity, migrated from
kernel/temporal/activities.py. Runs outside the kernel trust boundary.

Session decisions folded in:
- Q1: Harness owns completion via `indemn queue complete` / `indemn queue fail`
- Q2: complete | failed only, no needs_human
- G1.4 refined: 4 middleware (no HITL for async)
- Q3: Three-layer LLM config merge (Runtime + Associate + Deployment)
- Q4: Skill hash verification in CLI (harness trusts CLI surface)
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from harness.agent import build_agent
from harness.cron_runner import run_cron_skill
from harness.completion_logic import agent_did_useful_work
from harness_common.cli import CLIError, indemn
from harness.trace_helpers import serialize_messages, serialize_run_tree, derive_child_runs, aggregate_tokens
from langchain_core.tracers.run_collector import RunCollectorCallbackHandler
from harness_common.runtime import RUNTIME_ID, heartbeat_loop, register_instance
from indemn_os.types import AgentExecutionInput, AgentExecutionResult
from temporalio import activity
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.worker import Worker

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

TASK_QUEUE = f"runtime-{RUNTIME_ID}"


def _merge_llm_config(runtime: dict, associate: dict, deployment: dict | None) -> dict:
    """Three-layer config merge per Phase 4-5 spec § 5.3 [G-50].

    Runtime defaults -> Associate override -> Deployment override.
    Shallow merge, last-writer-wins. Deployment layer pass-through when absent.
    """
    return {
        **(runtime.get("llm_config") or {}),
        **(associate.get("llm_config") or {}),
        **((deployment.get("llm_override") or {}) if deployment else {}),
    }


_TRUNCATE_THRESHOLD = 1000


def _truncate_large_fields(data: dict, threshold: int = _TRUNCATE_THRESHOLD) -> dict:
    """Truncate large string fields in entity context to keep initial LLM
    context lean. The agent can always read the full entity via CLI if
    it needs more detail.

    Modifies the dict in place and returns it."""
    if not isinstance(data, dict):
        return data
    for key, value in data.items():
        if isinstance(value, str) and len(value) > threshold:
            total = len(value)
            data[key] = (
                value[:threshold]
                + f"\n\n[… truncated — {total} chars total. "
                + f"Run `indemn {data.get('_entity_type', 'entity').lower()} get {data.get('_id', '<id>')}` "
                + "to read full content.]"
            )
        elif isinstance(value, dict):
            _truncate_large_fields(value, threshold)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _truncate_large_fields(item, threshold)
    return data


def _load_message_context(entity_type: str, entity_id: str, associate: dict) -> dict:
    """Build the agent's working context dict from the message's
    `(entity_type, entity_id)`.

    Watch-driven messages — `entity_type` is a real domain or kernel entity
    (`Email`, `Meeting`, `Touchpoint`, …) — load the focus entity via the CLI
    with `--depth 2 --include-related` so the agent has both forward and
    reverse relationship context for its working set.

    Synthetic kernel-internal messages — `entity_type` starts with `_` —
    skip the entity-load. The leading underscore is the kernel's convention
    for "this is not a real entity type" (currently `_scheduled` from
    `kernel/queue_processor.py::check_scheduled_associates`; reserved for
    future synthetic types like `_circuit_broken`, `_zombie_recovery`).
    There is no `indemn _<sentinel>` CLI command; running it would
    `CLIError`. Instead build a trigger descriptor — event name, the actor
    `entity_id` points at, plus the actor's identity and schedule — so the
    agent's prompt has structured context for what fired this run.

    Bug #41 fix shape (framing B): honor the `_` sentinel. Watch-driven
    behavior is unchanged; the helper just routes between the two cases.
    The Bug #41 row in `os-learnings.md` documents the full reasoning,
    including why this was preferred over framing A (changing the kernel
    sweep to `entity_type="Actor"`) and framing C (a separate
    `ScheduledActorWorkflow` with its own harness activity).
    """
    if entity_type.startswith("_"):
        return {
            "_synthetic": True,
            "trigger": entity_type,
            "trigger_entity_id": entity_id,
            "associate_id": associate.get("_id"),
            "associate_name": associate.get("name"),
            "trigger_schedule": associate.get("trigger_schedule"),
        }

    if entity_type == "Trace":
        trace = indemn("trace", "get", entity_id)
        return {
            "_entity_type": "Trace",
            "_id": trace.get("_id"),
            "associate_id": trace.get("associate_id"),
            "associate_name": trace.get("associate_name"),
            "entity_type": trace.get("entity_type"),
            "entity_id": trace.get("entity_id"),
            "correlation_id": trace.get("correlation_id"),
            "name": trace.get("name"),
            "execution_status": trace.get("execution_status"),
            "status": trace.get("status"),
            "prompt_tokens": trace.get("prompt_tokens"),
            "total_tokens": trace.get("total_tokens"),
            "duration_ms": trace.get("duration_ms"),
            "messages_count": len(trace.get("messages", [])),
            "child_runs_count": len(trace.get("child_runs", [])),
        }

    entity_slug = entity_type.lower()
    context = indemn(
        entity_slug, "get", entity_id, "--depth", "2", "--include-related"
    )
    return _truncate_large_fields(context)


async def _create_trace(
    input: AgentExecutionInput,
    associate: dict,
    messages: list,
    tools_used: list[str],
    langsmith_run_id: uuid.UUID,
    start_time: datetime,
    start_ts: float,
    collected_run=None,
    correlation_id: str | None = None,
    execution_status: str = "success",
    error_msg: str | None = None,
):
    """Create a durable Trace kernel entity after agent.ainvoke().

    Non-blocking (runs CLI via asyncio.to_thread). Failure is logged
    but does not block message completion.
    """
    end_time = datetime.now(timezone.utc)
    duration_ms = int((time.monotonic() - start_ts) * 1000)

    serialized = serialize_messages(messages)
    if collected_run:
        cr_children = getattr(collected_run, "child_runs", []) or []
        log.info("collect_runs captured: name=%s run_type=%s children=%d",
                 getattr(collected_run, "name", "?"), getattr(collected_run, "run_type", "?"), len(cr_children))
        child_runs = serialize_run_tree(collected_run)
        if not child_runs:
            log.warning("serialize_run_tree returned empty, falling back to derive_child_runs")
            child_runs = derive_child_runs(messages)
    else:
        log.info("collect_runs did not capture a run, using derive_child_runs")
        child_runs = derive_child_runs(messages)
    log.info("child_runs: %d items", len(child_runs))
    prompt_tokens, completion_tokens, total_tokens = aggregate_tokens(messages)

    trace_data = {
        "trace_id": str(langsmith_run_id),
        "langsmith_run_id": str(langsmith_run_id),
        "session_id": os.environ.get("LANGCHAIN_PROJECT"),
        "associate_id": str(input.associate_id),
        "associate_name": associate.get("name", ""),
        "message_id": str(input.message_id),
        "correlation_id": correlation_id,
        "entity_type": input.entity_type,
        "entity_id": str(input.entity_id),
        "name": f"{associate.get('name', 'agent')} → {input.entity_type} {str(input.entity_id)[:8]}",
        "run_type": "chain",
        "inputs": serialized[0] if serialized else {},
        "outputs": next(
            (m for m in reversed(serialized) if m.get("type") == "ai"),
            serialized[-1] if serialized else {},
        ),
        "messages": serialized,
        "child_runs": child_runs,
        "tags": [
            f"associate:{associate.get('name', 'unknown')}",
            f"entity_type:{input.entity_type}",
            f"runtime:{RUNTIME_ID}",
        ],
        "extra": {"rubric_ids": associate.get("rubric_ids", [])},
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "duration_ms": duration_ms,
        "execution_status": execution_status,
        "error": error_msg,
        "status": "created",
    }

    payload = json.dumps(trace_data, default=str)
    if len(payload) > 800_000:
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(payload)
            tmp_path = f.name
        try:
            await asyncio.to_thread(
                indemn, "trace", "create", "--data-file", tmp_path, timeout=60.0
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    else:
        await asyncio.to_thread(indemn, "trace", "create", "--data", payload, timeout=60.0)
    log.info("Trace created for %s -> %s %s", associate.get("name"), input.entity_type, str(input.entity_id)[:8])


async def _sync_eval_to_langsmith(trace_entity_id: str, evaluator_run_id: str | None = None):
    """Sync evaluation results to LangSmith after evaluator completes.

    Called when entity_type == "Trace" — the evaluator processed a Trace.
    Loads the Trace to get langsmith_run_id, loads EvaluationResults,
    and calls client.create_feedback() for each rubric score.
    """
    try:
        from langsmith import Client
    except ImportError:
        log.warning("langsmith not installed — skipping eval sync")
        return

    log.info("LangSmith sync: loading trace %s", trace_entity_id)
    trace = await asyncio.to_thread(indemn, "trace", "get", trace_entity_id, timeout=15.0)
    langsmith_run_id = trace.get("langsmith_run_id")
    if not langsmith_run_id:
        log.info("LangSmith sync: no langsmith_run_id on trace, skipping")
        return

    # Query evaluation results for this trace via API (not CLI filter
    # which may not support ObjectId relationship filtering)
    log.info("LangSmith sync: querying results for trace %s, ls_run=%s", trace_entity_id, langsmith_run_id)
    try:
        results = await asyncio.to_thread(
            indemn, "evaluationresult", "list",
            "--data", json.dumps({"trace_id": trace_entity_id}),
            timeout=15.0,
        )
    except CLIError as e:
        log.warning("LangSmith sync: failed to query results: %s", e)
        return

    if not results or not isinstance(results, list):
        log.info("LangSmith sync: no results found for trace %s (got %s)", trace_entity_id, type(results).__name__)
        return

    log.info("LangSmith sync: found %d results, syncing feedback", len(results))

    def _sync_feedback(ls_run_id, eval_results, source_run_id):
        from uuid import UUID
        client = Client()
        synced = 0
        ls_run_uuid = UUID(ls_run_id) if ls_run_id else None
        source_uuid = UUID(source_run_id) if source_run_id else None
        feedback_stats = {}

        for result in eval_results:
            eval_result_id = result.get("_id", "")
            eval_run_id = result.get("run_id", "")
            source_info = {}
            if eval_result_id:
                source_info["evaluation_result_id"] = eval_result_id
            if eval_run_id:
                source_info["evaluation_run_id"] = eval_run_id

            feedback_ids = []

            for score_entry in result.get("rubric_scores", []):
                try:
                    passed = score_entry.get("passed", False)
                    reasoning = score_entry.get("reasoning", "")
                    attribution = score_entry.get("failure_attribution")
                    recommendation = score_entry.get("recommendation")

                    comment_parts = [reasoning]
                    if attribution:
                        comment_parts.append(f"Attribution: {attribution}")
                    if recommendation:
                        comment_parts.append(f"Recommendation: {recommendation}")

                    fb = client.create_feedback(
                        run_id=ls_run_uuid,
                        trace_id=ls_run_uuid,
                        key=score_entry.get("rule_id", "unknown"),
                        score=score_entry.get("score", 0.0),
                        value="Pass" if passed else "Fail",
                        comment=" | ".join(p for p in comment_parts if p),
                        feedback_source_type="model",
                        source_run_id=source_uuid,
                        source_info=source_info if source_info else None,
                    )
                    if fb and hasattr(fb, "id"):
                        feedback_ids.append(str(fb.id))
                    synced += 1
                    rid = score_entry.get("rule_id", "unknown")
                    feedback_stats[rid] = {"score": score_entry.get("score", 0.0), "passed": passed}
                except Exception as e:
                    log.warning("LangSmith feedback failed for rule %s: %s",
                                score_entry.get("rule_id"), e)

            for criteria in result.get("criteria_scores", []):
                try:
                    fb = client.create_feedback(
                        run_id=ls_run_uuid,
                        trace_id=ls_run_uuid,
                        key=f"criteria:{criteria.get('criterion', 'unknown')}",
                        score=criteria.get("score", 0.0),
                        value="Pass" if criteria.get("passed") else "Fail",
                        comment=criteria.get("reasoning", ""),
                        feedback_source_type="model",
                        source_run_id=source_uuid,
                        source_info=source_info if source_info else None,
                    )
                    if fb and hasattr(fb, "id"):
                        feedback_ids.append(str(fb.id))
                    synced += 1
                    ckey = f"criteria:{criteria.get('criterion', 'unknown')}"
                    feedback_stats[ckey] = {"score": criteria.get("score", 0.0), "passed": criteria.get("passed", False)}
                except Exception as e:
                    log.warning("LangSmith feedback failed for criteria %s: %s",
                                criteria.get("criterion"), e)

            for check in result.get("outcome_checks", []):
                try:
                    fb = client.create_feedback(
                        run_id=ls_run_uuid,
                        trace_id=ls_run_uuid,
                        key=f"outcome:{check.get('check_name', 'unknown')}",
                        score=1.0 if check.get("passed") else 0.0,
                        value="Pass" if check.get("passed") else "Fail",
                        comment=json.dumps({"actual": check.get("actual_value"), "expected": check.get("expected")}),
                        feedback_source_type="model",
                        source_run_id=source_uuid,
                        source_info=source_info if source_info else None,
                    )
                    if fb and hasattr(fb, "id"):
                        feedback_ids.append(str(fb.id))
                    synced += 1
                    okey = f"outcome:{check.get('check_name', 'unknown')}"
                    feedback_stats[okey] = {"score": 1.0 if check.get("passed") else 0.0, "passed": check.get("passed", False)}
                except Exception as e:
                    log.warning("LangSmith feedback failed for outcome %s: %s",
                                check.get("check_name"), e)

            try:
                overall_passed = result.get("passed", False)
                fb = client.create_feedback(
                    run_id=ls_run_uuid,
                    trace_id=ls_run_uuid,
                    key="evaluation_passed",
                    score=1.0 if overall_passed else 0.0,
                    value="Pass" if overall_passed else "Fail",
                    comment="All rules passed" if overall_passed else "One or more rules failed",
                    feedback_source_type="model",
                    source_run_id=source_uuid,
                    source_info=source_info if source_info else None,
                )
                if fb and hasattr(fb, "id"):
                    feedback_ids.append(str(fb.id))
                synced += 1
                feedback_stats["evaluation_passed"] = {"score": 1.0 if overall_passed else 0.0, "passed": overall_passed}
            except Exception as e:
                log.warning("LangSmith feedback failed for evaluation_passed: %s", e)

            if feedback_ids and eval_result_id:
                try:
                    indemn("evaluationresult", "update", eval_result_id,
                           "--data", json.dumps({"langsmith_feedback_ids": feedback_ids}))
                    indemn("evaluationresult", "transition", eval_result_id, "--to", "synced")
                except CLIError as e:
                    log.warning("Failed to update EvaluationResult %s: %s", eval_result_id, e)

        log.info("LangSmith sync: %d feedback entries synced to run %s", synced, ls_run_id)

        try:
            indemn("trace", "update", trace_entity_id,
                   "--data", json.dumps({"feedback_stats": feedback_stats}))
        except CLIError as e:
            log.warning("Failed to update Trace.feedback_stats: %s", e)

    await asyncio.to_thread(_sync_feedback, langsmith_run_id, results, evaluator_run_id)


@activity.defn
async def process_with_associate(input: AgentExecutionInput) -> AgentExecutionResult:
    """Agent execution loop. Migrated from kernel/temporal/activities.py.

    Harness orchestration uses the CLI for I/O (load context, mark complete).
    Agent's own tool execution uses deepagents' built-in execute via backend.
    """
    _langsmith_run_id = uuid.uuid4()
    _start_time = datetime.now(timezone.utc)
    _start_ts = time.monotonic()
    associate: dict = {"name": "unknown"}
    _captured_messages: list = []
    _captured_tools: list[str] = []

    try:
        # Heartbeat immediately — before any CLI calls. Under concurrency
        # pressure, the 3-4 CLI subprocess calls below can take > 90s
        # (the heartbeat_timeout), causing Temporal to cancel the activity
        # before it even starts. This buys us the full 90s window.
        activity.heartbeat("loading_context")

        # Set causation message ID so downstream CLI calls propagate it
        os.environ["INDEMN_CAUSATION_MESSAGE_ID"] = str(input.message_id)
        # Set effective-actor-id (Bug #22 forensics): all CLI calls from this
        # activity will record this associate as the effective actor in the
        # changes collection, while the auth token stays the runtime's
        # Platform Admin equivalent. Cleaned up in finally below.
        os.environ["INDEMN_EFFECTIVE_ACTOR_ID"] = str(input.associate_id)

        # Load associate config + context (harness orchestration, not agent tools)
        associate = indemn("actor", "get", input.associate_id)

        if associate.get("status") != "active":
            log.warning(
                "Actor %s is %s at activity start — aborting",
                input.associate_id,
                associate.get("status"),
            )
            raise RuntimeError(
                f"Actor not active (status={associate.get('status')})"
            )

        # --- Shared heartbeat helper with actor-status check ---
        # Defined before cron_runner branch because both paths use it.
        async def _heartbeat_with_status_check(label: str):
            """Heartbeat loop that also checks actor status every cycle.

            If the actor has been suspended mid-run, raises RuntimeError
            to cancel the activity cleanly. The queue visibility sweep
            recovers the message and parks it (actor is now suspended).
            """
            while True:
                try:
                    await asyncio.sleep(30.0)
                    activity.heartbeat(label)
                    try:
                        await asyncio.to_thread(
                            indemn, "queue", "extend-visibility", str(input.message_id),
                        )
                    except CLIError:
                        pass
                    try:
                        actor_state = await asyncio.to_thread(
                            indemn, "actor", "get", str(input.associate_id),
                        )
                        if actor_state.get("status") != "active":
                            log.warning(
                                "Actor %s is %s — cancelling activity",
                                input.associate_id,
                                actor_state.get("status"),
                            )
                            raise RuntimeError(
                                f"Actor suspended (status={actor_state.get('status')})"
                            )
                    except CLIError:
                        pass
                except asyncio.CancelledError:
                    break

        if associate.get("mode") == "cron_runner":
            activity.heartbeat("starting_cron_runner")
            cron_heartbeat_task = None
            try:
                cron_heartbeat_task = asyncio.create_task(
                    _heartbeat_with_status_check("cron_runner_running")
                )
                try:
                    return await asyncio.to_thread(run_cron_skill, input, associate)
                finally:
                    cron_heartbeat_task.cancel()
                    try:
                        await cron_heartbeat_task
                    except (asyncio.CancelledError, RuntimeError):
                        pass
            finally:
                os.environ.pop("INDEMN_CAUSATION_MESSAGE_ID", None)
                os.environ.pop("INDEMN_EFFECTIVE_ACTOR_ID", None)

        # Bug #41: route between watch-driven entity load and synthetic
        # `_<sentinel>` trigger descriptor — see _load_message_context docstring.
        context = _load_message_context(input.entity_type, input.entity_id, associate)

        # Load Runtime for three-layer config merge
        runtime_id = associate.get("runtime_id", RUNTIME_ID)
        runtime = indemn("runtime", "get", str(runtime_id))

        # Load Deployment if session has one (common for chat/voice, rare for async)
        deployment = None
        deployment_id = associate.get("deployment_id")
        if deployment_id:
            deployment = indemn("deployment", "get", str(deployment_id))

        # Three-layer LLM config merge [Q3, G-50]
        llm_config = _merge_llm_config(runtime, associate, deployment)

        # Per-activity sandbox dir for the LocalShellBackend (Bug #3 fix:
        # scopes /workspace/{activity_id}/ so one agent's tool-cache doesn't
        # leak into another's). The agent loads its operating + entity skills
        # at runtime via `execute('indemn skill get <name>')` per the
        # build_system_prompt directive — no filesystem SKILL.md writes here.
        activity_id = f"act-{input.message_id}"

        agent = build_agent(
            associate=associate,
            llm_config=llm_config,
            activity_id=activity_id,
        )

        # Heartbeat before the potentially long agent run
        activity.heartbeat("starting_agent")

        # Reset timing to exclude setup overhead (context loading, config merge)
        _start_time = datetime.now(timezone.utc)
        _start_ts = time.monotonic()

        # Run the agent loop with periodic heartbeating.
        # ainvoke() may take minutes; heartbeat every 30s to prevent
        # Temporal from cancelling the activity.
        heartbeat_task = None
        _run_collector = RunCollectorCallbackHandler()
        try:
            heartbeat_task = asyncio.create_task(
                _heartbeat_with_status_check("agent_running")
            )

            result = await agent.ainvoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": f"Process this work:\n\n{context}",
                        }
                    ],
                },
                config={
                    "run_id": _langsmith_run_id,
                    "recursion_limit": 50,
                    "callbacks": [_run_collector],
                    "metadata": {
                        "associate_id": str(input.associate_id),
                        "associate_name": associate.get("name"),
                        "message_id": str(input.message_id),
                        "entity_type": input.entity_type,
                        "entity_id": str(input.entity_id),
                        "runtime_id": str(runtime_id),
                        "correlation_id": input.correlation_id,
                        "thread_id": input.correlation_id,
                    },
                    "tags": [
                        f"associate:{associate.get('name', 'unknown')}",
                        f"entity_type:{input.entity_type}",
                        f"runtime:{RUNTIME_ID}",
                    ],
                    "run_name": f"{associate.get('name', 'agent')} → {input.entity_type} {str(input.entity_id)[:8]}",
                },
            )
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except (asyncio.CancelledError, RuntimeError):
                    pass

        # Log what the agent did — every message, every tool call
        messages = result.get("messages", [])
        _captured_messages = messages
        tools_used = []
        for msg in messages:
            msg_type = getattr(msg, "type", type(msg).__name__)
            if msg_type == "tool":
                tool_name = getattr(msg, "name", "unknown")
                tools_used.append(tool_name)
                content = str(getattr(msg, "content", ""))[:500]
                log.info("Agent tool result [%s]: %s", tool_name, content)
            elif msg_type == "ai":
                # Log tool calls the AI made
                tool_calls = getattr(msg, "tool_calls", [])
                for tc in tool_calls:
                    tc_name = (
                        tc.get("name", "unknown")
                        if isinstance(tc, dict)
                        else getattr(tc, "name", "unknown")
                    )
                    tc_args = (
                        tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                    )
                    log.info("Agent called tool [%s]: %s", tc_name, str(tc_args)[:500])
                if not tool_calls:
                    log.info("Agent response: %s", str(getattr(msg, "content", ""))[:300])

        _captured_tools = tools_used
        log.info("Agent completed: %d messages, tools=%s", len(messages), tools_used)

        # Rebuild the run tree from the flat traced_runs list.
        # RunCollectorCallbackHandler persists copies with shallow child_runs.
        # Convert to plain dicts and reconstruct the parent-child hierarchy.
        _collected_run = None
        if _run_collector.traced_runs:
            from types import SimpleNamespace
            flat = []
            for r in _run_collector.traced_runs:
                flat.append({
                    "id": str(getattr(r, "id", "")),
                    "parent_run_id": str(getattr(r, "parent_run_id", "")) if getattr(r, "parent_run_id", None) else None,
                    "name": getattr(r, "name", "unknown"),
                    "run_type": getattr(r, "run_type", "chain"),
                    "inputs": getattr(r, "inputs", {}) or {},
                    "outputs": getattr(r, "outputs", {}) or {},
                    "error": getattr(r, "error", None),
                    "start_time": getattr(r, "start_time", None),
                    "end_time": getattr(r, "end_time", None),
                    "child_runs": [],
                })

            by_id = {d["id"]: d for d in flat}
            for d in flat:
                if d["parent_run_id"] and d["parent_run_id"] in by_id:
                    by_id[d["parent_run_id"]]["child_runs"].append(d)

            root_dict = None
            for d in flat:
                if d["parent_run_id"] is None:
                    root_dict = d
                    break

            if root_dict:
                _collected_run = SimpleNamespace(**root_dict)
                # Recursively convert children to SimpleNamespace
                def _to_ns(d):
                    ns = SimpleNamespace(**d)
                    ns.child_runs = [_to_ns(c) for c in d.get("child_runs", [])]
                    return ns
                _collected_run = _to_ns(root_dict)
                log.info("RunCollector: %d runs, root=%s children=%d",
                         len(flat), root_dict["name"],
                         len(root_dict["child_runs"]))

        # Create durable Trace entity (non-blocking)
        try:
            await _create_trace(
                input, associate, messages, tools_used,
                _langsmith_run_id, _start_time, _start_ts,
                collected_run=_collected_run,
                correlation_id=input.correlation_id,
            )
        except Exception as e:
            log.warning("Trace creation failed (non-blocking): %s", e)

        # Bug #2: detect agent that ran-but-did-nothing. Without this check the
        # harness used to silently mark complete even when the agent produced
        # no output and made no mutating CLI calls — message stayed in
        # `processing` indefinitely (Apr 24 GR Little Extractor trace).
        did_useful_work, no_work_reason = agent_did_useful_work(messages)

        # Clean up causation env var
        os.environ.pop("INDEMN_CAUSATION_MESSAGE_ID", None)
        os.environ.pop("INDEMN_EFFECTIVE_ACTOR_ID", None)

        if did_useful_work:
            # Sync evaluation results to LangSmith for evaluator runs
            if input.entity_type == "Trace":
                try:
                    await _sync_eval_to_langsmith(
                        str(input.entity_id),
                        evaluator_run_id=str(_langsmith_run_id),
                    )
                except Exception as e:
                    log.warning("LangSmith eval sync failed (non-blocking): %s", e)

            indemn("queue", "complete", input.message_id)
            return AgentExecutionResult(
                status="complete",
                iterations=len(messages),
                tools_used=tools_used,
            )
        else:
            log.warning(
                "Agent produced no useful work for message %s: %s",
                input.message_id,
                no_work_reason,
            )
            indemn("queue", "fail", input.message_id, "--reason", no_work_reason)
            return AgentExecutionResult(
                status="failed",
                iterations=len(messages),
                tools_used=tools_used,
                error=no_work_reason,
            )

    except Exception as e:
        try:
            await _create_trace(
                input, associate, _captured_messages, _captured_tools,
                _langsmith_run_id, _start_time, _start_ts,
                correlation_id=input.correlation_id,
                execution_status="error", error_msg=str(e)[:500],
            )
        except Exception:
            pass
        # Clean up causation env var
        os.environ.pop("INDEMN_CAUSATION_MESSAGE_ID", None)
        os.environ.pop("INDEMN_EFFECTIVE_ACTOR_ID", None)
        # Mark the message failed — harness owns failure reporting [Q1]
        try:
            indemn("queue", "fail", input.message_id, "--reason", str(e)[:500])
        except CLIError:
            log.warning("Failed to report message failure via CLI: %s", e)
        raise  # Re-raise so Temporal marks the activity failed

    finally:
        # Sandbox directories accumulate in /workspace/act-{id}/ during
        # the container's lifetime. This is acceptable — the container is
        # ephemeral (dies on every Railway deploy). Aggressive per-activity
        # cleanup was the root cause of the 8.8M token CE spiral: when two
        # activities shared a truncated directory name, one's cleanup deleted
        # the other's workspace mid-execution. Even with full message_id
        # uniqueness (commit 6387bec), deleting during `finally` is fragile
        # against retries and concurrent edge cases. Let the container
        # lifecycle handle it.
        pass


def _setup_gcp_credentials():
    """Write GCP service account JSON to file if provided via env var.

    Fixes escaped newlines in PEM keys — Railway env vars store \\n as
    literal backslash-n, but PEM needs actual newlines.
    """
    sa_json = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "")
    if sa_json:
        import json as json_mod

        try:
            data = json_mod.loads(sa_json)
            if "private_key" in data:
                data["private_key"] = data["private_key"].replace("\\n", "\n")
            data.setdefault("type", "service_account")
            data.setdefault("auth_uri", "https://accounts.google.com/o/oauth2/auth")
            data.setdefault("token_uri", "https://oauth2.googleapis.com/token")
            data.setdefault("universe_domain", "googleapis.com")
            sa_json = json_mod.dumps(data)
        except Exception as e:
            log.warning("Failed to parse GCP SA JSON: %s", e)

        sa_path = "/tmp/gcp-sa.json"
        with open(sa_path, "w") as f:
            f.write(sa_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = sa_path
        log.info("GCP credentials written to %s", sa_path)


async def main():
    log.info("Starting async-deepagents harness, runtime=%s", RUNTIME_ID)
    log.info("Sandbox type: %s", os.environ.get("INDEMN_SANDBOX_TYPE", "localshell"))

    # Increase thread pool for high concurrency — deepagents uses asyncio.to_thread
    # for filesystem ops (skill loading, backend ls/read_file). Default pool (5 threads
    # per CPU) starves at 50+ concurrent agents.
    from concurrent.futures import ThreadPoolExecutor
    import asyncio as _asyncio
    _asyncio.get_event_loop().set_default_executor(ThreadPoolExecutor(max_workers=500))

    _setup_gcp_credentials()
    await register_instance()

    connect_kwargs = {
        "target_host": os.environ["TEMPORAL_ADDRESS"],
        "namespace": os.environ.get("TEMPORAL_NAMESPACE", "default"),
    }
    api_key = os.environ.get("TEMPORAL_API_KEY", "")
    if api_key:
        connect_kwargs["api_key"] = api_key
        connect_kwargs["tls"] = True  # Temporal Cloud requires TLS
    client = await Client.connect(**connect_kwargs)

    # Read capacity from Runtime config (not hardcoded)
    try:
        runtime_config = indemn("runtime", "get", RUNTIME_ID)
        max_concurrent = runtime_config.get("capacity", {}).get("max_concurrent_sessions") or 10
    except Exception:
        max_concurrent = 10  # Fallback if Runtime config unavailable at startup

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        activities=[process_with_associate],
        max_concurrent_activities=max_concurrent,
        interceptors=[TracingInterceptor()],
        graceful_shutdown_timeout=timedelta(seconds=120),
    )

    log.info("Worker listening on queue: %s (max_concurrent=%d)", TASK_QUEUE, max_concurrent)

    await asyncio.gather(
        worker.run(),
        heartbeat_loop(interval_s=30.0),
    )


if __name__ == "__main__":
    asyncio.run(main())
