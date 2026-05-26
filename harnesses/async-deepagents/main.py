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
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from harness.agent import build_agent
from harness.cron_runner import run_cron_skill
from harness_common.cli import CLIError, indemn
from harness.trace_helpers import serialize_messages, serialize_run_tree, derive_child_runs, aggregate_tokens
from harness_common.thread_id import derive_checkpointer_thread_id
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tracers.run_collector import RunCollectorCallbackHandler
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.mongodb import MongoDBSaver
from motor.motor_asyncio import AsyncIOMotorClient
from harness_common.runtime import RUNTIME_ID, heartbeat_loop, register_instance
from indemn_os.types import AgentExecutionInput, AgentExecutionResult
from temporalio import activity
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.worker import Worker

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

TASK_QUEUE = f"runtime-{RUNTIME_ID}"

# AI-407 §15.4: Phase-4 MongoDBSaver checkpointer for async harness.
# Adapted from chat-deepagents' lifespan-init pattern; async-deepagents uses
# Temporal worker (no Starlette lifespan event), so we lazy-init on first use
# with a False-sentinel for "tried and failed — degraded to MemorySaver fallback".
# Mirrors voice harness Task 2.16 (an asyncio.Lock there guards concurrent room
# dispatches; here Temporal activities are concurrent in the same event loop,
# but the worst case is duplicate MotorClient construction on first race — not
# catastrophic. Keep simple per playbook spec.)
_checkpointer = None


async def _get_or_init_checkpointer():
    """Return the module-level MongoDBSaver, lazily initialized.

    Returns None if MONGODB_URI is absent or the ping fails — caller falls
    back to MemorySaver so the activity doesn't block on missing infra
    (mirrors chat-deepagents degradation pattern).
    """
    global _checkpointer
    # `False` sentinel = "tried + failed; don't keep retrying within process lifetime"
    if _checkpointer is False:
        return None
    if _checkpointer is not None:
        return _checkpointer
    mongodb_uri = os.environ.get("MONGODB_URI", "")
    if not mongodb_uri:
        log.warning(
            "MONGODB_URI not set — async checkpointer disabled "
            "(falling back to MemorySaver; no human-in-the-loop pause/resume)"
        )
        _checkpointer = False
        return None
    try:
        motor_client = AsyncIOMotorClient(mongodb_uri)
        await motor_client.admin.command("ping")
        _checkpointer = MongoDBSaver(
            motor_client.delegate, db_name="indemn_os_checkpoints"
        )
        log.info("Async MongoDB checkpointer initialized")
        return _checkpointer
    except Exception as e:
        log.warning(
            "MongoDB checkpointer unavailable: %s — degraded to MemorySaver", e
        )
        _checkpointer = False
        return None


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


def _format_xml_value(value, indent=1) -> list[str]:
    """Recursively format a value as XML lines."""
    prefix = "  " * indent
    if value is None or value == "" or value == [] or value == {}:
        return []
    if isinstance(value, dict):
        lines = []
        for dk, dv in value.items():
            inner = _format_xml_value(dv, indent + 1)
            if not inner:
                continue
            if len(inner) == 1 and not inner[0].strip().startswith("<"):
                lines.append(f"{prefix}<{dk}>{inner[0].strip()}</{dk}>")
            else:
                lines.append(f"{prefix}<{dk}>")
                lines.extend(inner)
                lines.append(f"{prefix}</{dk}>")
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                lines.append(f"{prefix}<entry>")
                lines.extend(_format_xml_value(item, indent + 1))
                lines.append(f"{prefix}</entry>")
            else:
                lines.append(f"{prefix}<item>{item}</item>")
        return lines
    s = str(value)
    if len(s) > 200:
        return [s]
    return [f"{prefix}{s}"]


def _format_entity_xml(data: dict, entity_type: str) -> str:
    """Format entity context dict as XML."""
    if not isinstance(data, dict):
        return str(data)

    tag = entity_type.lower() if entity_type else "entity"
    entity_id = data.get("_id", "")

    lines = [f"<{tag} id=\"{entity_id}\">"]
    for k, v in data.items():
        inner = _format_xml_value(v, 2)
        if not inner:
            continue
        if len(inner) == 1 and not inner[0].strip().startswith("<"):
            lines.append(f"  <{k}>{inner[0].strip()}</{k}>")
        else:
            lines.append(f"  <{k}>")
            lines.extend(inner)
            lines.append(f"  </{k}>")
    lines.append(f"</{tag}>")
    return "\n".join(lines)


def _build_skill_section_xml(associate: dict) -> str:
    """Build the skill section XML for the Phase-4 <skill> SystemMessage.

    AI-407 §15.5: replaces the Phase-3 user-message `<skill>` block with a
    dedicated SystemMessage composed by compose_initial_messages. This helper
    fetches the associate's operating skill(s) and emits per-skill blocks
    `<skill name="X">...content...</skill>` joined by blank lines. The caller
    (compose_initial_messages) wraps the result in an outer <skill>...</skill>
    when constructing the SystemMessage so the agent's DEFAULT_PROMPT
    "<skill> SystemMessage" reference is satisfied.

    Returns empty string when associate has no skills (multi-skill operating
    set; cron_runner mode skips this path entirely).
    """
    parts: list[str] = []
    skill_refs = associate.get("skills") or []
    for ref in skill_refs:
        try:
            skill = indemn("skill", "get", ref)
            content = skill.get("content", "") if isinstance(skill, dict) else str(skill)
            parts.append(f'<skill name="{ref}">')
            parts.append(content)
            parts.append("</skill>")
            parts.append("")
        except CLIError as e:
            log.warning("Failed to load skill %s: %s", ref, e)
    return "\n".join(parts).rstrip()


def compose_initial_messages(skill_content: str, entity_xml: str) -> list:
    """Compose the initial agent input messages for a single async invocation.

    Phase 4 (AI-407 §15.5): the operating skill is a SystemMessage; the entity
    context is the HumanMessage. Mirrors real-time harness composition (chat,
    voice). The agent's DEFAULT_PROMPT references "<skill> SystemMessage" —
    this function produces it.
    """
    return [
        SystemMessage(content=f"<skill>\n{skill_content}\n</skill>"),
        HumanMessage(content=entity_xml),
    ]


def build_runnable_config(input, associate: dict, runtime_id) -> dict:
    """Compose the LangChain RunnableConfig per AI-407 §13.5 (async harness).

    Two-part contract from §13.2:
      - LangSmith `metadata.thread_id` = correlation_id (UI grouping by lineage —
        async cascades show as one thread, real-time sessions show as one thread,
        cross-channel chains show as one thread).
      - LangGraph `configurable.thread_id` = derive_checkpointer_thread_id(ctx)
        (checkpointer persistence key — per-message isolation for cascades, or
        per-Interaction continuity for handoff cases).

    Field-name adapter: AgentExecutionInput uses `entity_type` / `entity_id`
    (the §13 work_context's `target_entity_type` / `target_entity_id`).
    """
    from types import SimpleNamespace

    work_ctx = SimpleNamespace(
        is_real_time_session=False,  # async harness — by definition
        interaction_id=None,
        target_entity_type=input.entity_type,
        target_entity_id=input.entity_id,
        message_id=input.message_id,
    )
    checkpointer_thread_id = derive_checkpointer_thread_id(work_ctx)

    associate_name = associate.get("name")
    associate_id = associate.get("_id") or input.associate_id

    return {
        "configurable": {"thread_id": str(checkpointer_thread_id)},
        "metadata": {
            "thread_id": input.correlation_id,  # LangSmith UI grouping
            "correlation_id": input.correlation_id,
            "message_id": str(input.message_id),
            "interaction_id": (
                str(input.entity_id)
                if input.entity_type == "Interaction"
                else None
            ),
            "associate_id": str(associate_id),
            "associate_name": associate_name,
            "entity_type": input.entity_type,
            "entity_id": str(input.entity_id),
            "runtime_id": str(runtime_id),
        },
        "tags": [
            f"associate:{associate_name or 'unknown'}",
            "channel:async",
            f"runtime:{runtime_id}",
        ],
        "run_name": (
            f"{associate_name or 'agent'} → {input.entity_type} "
            f"{str(input.entity_id)[:8]}"
        ),
    }


def _load_message_context(entity_type: str, entity_id: str, associate: dict) -> dict:
    """Load the entity the associate will process.

    One path for all entity types — load the full entity with
    relationships. Safety truncation at 20K per field prevents
    dangerously large content injection.

    Synthetic kernel-internal messages (`_scheduled`, etc.) get a
    trigger descriptor instead of entity data.
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

    entity_slug = entity_type.lower()
    context = indemn(
        entity_slug,
        "get",
        entity_id,
        "--depth",
        "3",
        "--include-related",
        # Per-field truncation policy is the kernel's job now (driven by
        # FieldDefinition.content_size_hint + ?context_profile=llm). The
        # harness no longer applies field-length caps client-side.
        "--context-profile",
        "llm",
    )

    if entity_type == "Trace":
        # NOTE: this Trace-field pop is SEPARATE from the per-field
        # content_size_hint policy. These are JSON-structured fields
        # (lists of dicts), not plain strings — the kernel's hint-driven
        # truncation can't reach them. The harness still drops them here
        # to avoid injecting raw LLM execution data into a downstream
        # LLM's system prompt. Do not delete this pop.
        for field in ("inputs", "outputs", "child_runs"):
            context.pop(field, None)

    msg_id = os.environ.get("INDEMN_CAUSATION_MESSAGE_ID")
    if msg_id:
        try:
            msg = indemn("queue", "get", msg_id)
            msg_context = msg.get("context") or {}
            if msg_context.get("run_id"):
                context["run_id"] = msg_context["run_id"]
            if msg_context.get("rubric_ids"):
                context["rubric_ids"] = msg_context["rubric_ids"]
        except CLIError:
            pass

    return context


# Bug C1 — valid correlation_id forms across the system: UUID4 ("xxxxxxxx-xxxx-..."),
# hex32 (32 hex chars, no dashes), ObjectId hex (24 chars). All composed of
# [0-9a-fA-F-] only. The pre-fix Trace collection had ~0.5% binary correlation_ids
# (raw 16-byte OTEL TraceId stored as UTF-8 with replacement chars) from a
# since-removed code path. Now that A+B propagate correlation_id through every
# CLI subprocess in the cascade, a corrupt value on input.correlation_id would
# spread to every change record. This pattern catches the corruption.
_VALID_CORRELATION_ID_RE = re.compile(r"^[0-9a-fA-F-]+$")


def _normalize_correlation_id(cid) -> str | None:
    """Defensive guard against non-hex correlation_ids reaching the cascade.

    Returns the input unchanged if it's a valid hex/UUID-shaped string. Otherwise
    logs a warning and returns a fresh UUID so the cascade still gets traceability
    without propagating corruption. None input passes through (callers may pass
    None when correlation_id is unknown; downstream code handles missing values).
    """
    if cid is None:
        return None
    if not isinstance(cid, str):
        log.warning("Non-string correlation_id (type=%s); generating fresh UUID", type(cid).__name__)
        return str(uuid.uuid4())
    if not _VALID_CORRELATION_ID_RE.fullmatch(cid):
        log.warning(
            "Non-hex correlation_id (len=%d, repr=%r); generating fresh UUID — Bug C1 defense",
            len(cid), cid[:40],
        )
        return str(uuid.uuid4())
    return cid


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
    correlation_id = _normalize_correlation_id(correlation_id)
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

    # Always write the payload to a tempfile and pass via --data-file. Linux's
    # MAX_ARG_STRLEN caps a single argv entry at 128KB regardless of ARG_MAX
    # total; Trace payloads frequently exceed that (full conversation messages
    # + child_runs + entity inputs). The prior 200K threshold was wrong — any
    # payload between 128K and 200K hit [Errno 7] Argument list too long with
    # the Trace creation silently lost (non-blocking warning, no retry). One
    # code path eliminates the bug class. Trace creation is non-hot; tempfile
    # overhead (low microseconds) is irrelevant. See os-learnings.md.
    import tempfile
    payload = json.dumps(trace_data, default=str)
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

        # Delete old feedback from previous evaluations of this trace.
        # Without this, re-evaluation accumulates stale entries in LangSmith
        # (e.g. removed rubric rules still show as "Fail").
        if ls_run_uuid:
            try:
                existing_feedback = list(client.list_feedback(run_ids=[ls_run_uuid]))
                for old_fb in existing_feedback:
                    try:
                        client.delete_feedback(old_fb.id)
                    except Exception as e:
                        log.warning("Failed to delete old feedback %s: %s", old_fb.id, e)
                if existing_feedback:
                    log.info("LangSmith sync: deleted %d old feedback entries", len(existing_feedback))
            except Exception as e:
                log.warning("Failed to list old feedback: %s", e)

        for result in eval_results:
            eval_result_id = result.get("_id", "")
            eval_run_id = result.get("run_id", "")
            source_info = {}
            if eval_result_id:
                source_info["evaluation_result_id"] = eval_result_id
            if eval_run_id:
                source_info["evaluation_run_id"] = eval_run_id

            feedback_ids = []

            for score_entry in (result.get("rubric_scores") or []):
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

            for criteria in (result.get("criteria_scores") or []):
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

            for check in (result.get("outcome_checks") or []):
                try:
                    check_key = check.get("rule_id") or check.get("check_name") or "unknown"
                    fb = client.create_feedback(
                        run_id=ls_run_uuid,
                        trace_id=ls_run_uuid,
                        key=f"outcome:{check_key}",
                        score=1.0 if check.get("passed") else 0.0,
                        value="Pass" if check.get("passed") else "Fail",
                        comment=check.get("reasoning", ""),
                        feedback_source_type="model",
                        source_run_id=source_uuid,
                        source_info=source_info if source_info else None,
                    )
                    if fb and hasattr(fb, "id"):
                        feedback_ids.append(str(fb.id))
                    synced += 1
                    okey = f"outcome:{check_key}"
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
    agent = None
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
        # Cascade correlation_id: every CLI call in this activity propagates
        # the inbound message's correlation_id via X-Correlation-ID header,
        # so all entity changes + watch-fired messages downstream share one
        # id queryable via `indemn trace cascade <id>`. Normalized via
        # _normalize_correlation_id so binary/corrupt input doesn't spread
        # (Bug C1 defense).
        _norm_cid = _normalize_correlation_id(input.correlation_id)
        if _norm_cid:
            os.environ["INDEMN_CORRELATION_ID"] = _norm_cid

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
                os.environ.pop("INDEMN_CORRELATION_ID", None)

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

        # AI-407 §15.4: MongoDBSaver checkpointer (durable async state for
        # human-in-the-loop pause/resume). Falls back to MemorySaver if
        # MongoDB unavailable so the activity never blocks on missing infra.
        checkpointer = await _get_or_init_checkpointer()
        if checkpointer is None:
            checkpointer = MemorySaver()

        agent = build_agent(
            associate=associate,
            llm_config=llm_config,
            checkpointer=checkpointer,
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

            # AI-407 Phase 4: compose <skill> as SystemMessage + entity as HumanMessage
            # (replaces the Phase 3 single-HumanMessage with <context><skill>...</context>
            # composition via _build_context_with_skills). The agent's Phase-4
            # DEFAULT_PROMPT references "<skill> SystemMessage" + "<entity> reference".
            skill_xml = _build_skill_section_xml(associate)
            entity_xml = _format_entity_xml(context, input.entity_type)
            initial_messages = compose_initial_messages(skill_xml, entity_xml)

            # AI-407 §13.5: build_runnable_config encapsulates the thread_id rule
            # (configurable = derived per §13.3; metadata.thread_id = correlation_id)
            # plus all LangSmith metadata. Per-invocation overrides (run_id,
            # recursion_limit, callbacks) are added on top.
            runnable_config = build_runnable_config(input, associate, runtime_id)
            runnable_config["run_id"] = _langsmith_run_id
            runnable_config["recursion_limit"] = 200
            runnable_config["callbacks"] = [_run_collector]

            result = await agent.ainvoke(
                {"messages": initial_messages},
                config=runnable_config,
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

        # Create durable Trace entity (non-blocking).
        # Pass _norm_cid (already normalized at activity entry) so the Trace's
        # correlation_id matches the value propagated to subprocesses via
        # INDEMN_CORRELATION_ID — one cascade root, used everywhere.
        try:
            await _create_trace(
                input, associate, messages, tools_used,
                _langsmith_run_id, _start_time, _start_ts,
                collected_run=_collected_run,
                correlation_id=_norm_cid,
            )
        except Exception as e:
            log.warning("Trace creation failed (non-blocking): %s", e)

        # Clean up causation + effective-actor + correlation env vars
        os.environ.pop("INDEMN_CAUSATION_MESSAGE_ID", None)
        os.environ.pop("INDEMN_EFFECTIVE_ACTOR_ID", None)
        os.environ.pop("INDEMN_CORRELATION_ID", None)

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

    except Exception as e:
        if agent:
            try:
                state = await agent.aget_state(
                    {"configurable": {"thread_id": str(input.message_id)}}
                )
                if state and state.values:
                    _captured_messages = state.values.get("messages", [])
                    log.info("Recovered %d messages from checkpoint after error", len(_captured_messages))
            except Exception as recovery_err:
                log.warning("Checkpoint message recovery failed: %s", recovery_err)
        try:
            # _norm_cid may be undefined if we errored before activity-entry env
            # setup. Fall back to input.correlation_id (normalized in _create_trace).
            _err_cid = locals().get("_norm_cid") or input.correlation_id
            await _create_trace(
                input, associate, _captured_messages, _captured_tools,
                _langsmith_run_id, _start_time, _start_ts,
                correlation_id=_err_cid,
                execution_status="error", error_msg=str(e)[:500],
            )
        except Exception:
            pass
        # Clean up causation + effective-actor + correlation env vars
        os.environ.pop("INDEMN_CAUSATION_MESSAGE_ID", None)
        os.environ.pop("INDEMN_EFFECTIVE_ACTOR_ID", None)
        os.environ.pop("INDEMN_CORRELATION_ID", None)
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
