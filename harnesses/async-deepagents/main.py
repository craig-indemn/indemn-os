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
import logging
import os
from datetime import timedelta

from harness.agent import build_agent
from harness.cron_runner import run_cron_skill
from harness.completion_logic import agent_did_useful_work
from harness_common.cli import CLIError, indemn
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

    entity_slug = entity_type.lower()
    return indemn(
        entity_slug, "get", entity_id, "--depth", "2", "--include-related"
    )


@activity.defn
async def process_with_associate(input: AgentExecutionInput) -> AgentExecutionResult:
    """Agent execution loop. Migrated from kernel/temporal/activities.py.

    Harness orchestration uses the CLI for I/O (load context, mark complete).
    Agent's own tool execution uses deepagents' built-in execute via backend.
    """
    try:
        # Set causation message ID so downstream CLI calls propagate it
        os.environ["INDEMN_CAUSATION_MESSAGE_ID"] = str(input.message_id)
        # Set effective-actor-id (Bug #22 forensics): all CLI calls from this
        # activity will record this associate as the effective actor in the
        # changes collection, while the auth token stays the runtime's
        # Platform Admin equivalent. Cleaned up in finally below.
        os.environ["INDEMN_EFFECTIVE_ACTOR_ID"] = str(input.associate_id)

        # Load associate config + context (harness orchestration, not agent tools)
        associate = indemn("actor", "get", input.associate_id)

        # Bug #40: cron_runner mode bypasses the LLM agent entirely. The actor's
        # first skill carries a literal `## Command` CLI line; we shell-exec it
        # directly. No deepagents, no tool-call serialization, no LLM tokens.
        # run_cron_skill validates that the trigger is a synthetic `_*` event
        # and fails the message if something else routed here. The env-var
        # propagation (INDEMN_CAUSATION_MESSAGE_ID + INDEMN_EFFECTIVE_ACTOR_ID
        # set above) flows to the subprocess for forensics.
        #
        # Heartbeat: run_cron_skill calls a blocking subprocess (`indemn email
        # fetch-new` etc.) which can exceed Temporal's 90s heartbeat_timeout
        # under load (Slack with rate-limited channels, Drive on first crawl,
        # etc.). Without heartbeating, Temporal cancels the activity, the
        # workflow ends FAILED, the dispatch sweep marks the message
        # dead_letter via Bug #38 cleanup. Pre-fix: 11 spurious dead_letters
        # over 3 days (5 Email, 6 Slack — Slack-heavy because slack fetches
        # took longer). Fix: wrap the sync run_cron_skill in
        # `asyncio.to_thread` and run a heartbeat loop concurrently — same
        # shape as the agent path's `_heartbeat_loop` below. The heartbeat
        # is an activity-level concern (not a cron_runner concern), so it
        # lives here in the caller, keeping cron_runner.py sync + simple.
        if associate.get("mode") == "cron_runner":
            activity.heartbeat("starting_cron_runner")
            cron_heartbeat_task = None
            try:
                async def _cron_heartbeat_loop():
                    while True:
                        try:
                            await asyncio.sleep(30.0)
                            activity.heartbeat("cron_runner_running")
                        except asyncio.CancelledError:
                            break

                cron_heartbeat_task = asyncio.create_task(_cron_heartbeat_loop())
                try:
                    return await asyncio.to_thread(run_cron_skill, input, associate)
                finally:
                    cron_heartbeat_task.cancel()
                    try:
                        await cron_heartbeat_task
                    except asyncio.CancelledError:
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
        activity_id = f"act-{input.message_id[:12]}"

        agent = build_agent(
            associate=associate,
            llm_config=llm_config,
            activity_id=activity_id,
        )

        # Heartbeat before the potentially long agent run
        activity.heartbeat("starting_agent")

        # Run the agent loop with periodic heartbeating.
        # ainvoke() may take minutes; heartbeat every 30s to prevent
        # Temporal from cancelling the activity.
        heartbeat_task = None
        try:
            async def _heartbeat_loop():
                while True:
                    try:
                        await asyncio.sleep(30.0)
                        activity.heartbeat("agent_running")
                    except asyncio.CancelledError:
                        break

            heartbeat_task = asyncio.create_task(_heartbeat_loop())

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
                    "metadata": {
                        "associate_id": str(input.associate_id),
                        "associate_name": associate.get("name"),
                        "message_id": str(input.message_id),
                        "entity_type": input.entity_type,
                        "entity_id": str(input.entity_id),
                        "runtime_id": str(runtime_id),
                        "correlation_id": os.environ.get("INDEMN_CAUSATION_MESSAGE_ID"),
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
                except asyncio.CancelledError:
                    pass

        # Log what the agent did — every message, every tool call
        messages = result.get("messages", [])
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

        log.info("Agent completed: %d messages, tools=%s", len(messages), tools_used)

        # Bug #2: detect agent that ran-but-did-nothing. Without this check the
        # harness used to silently mark complete even when the agent produced
        # no output and made no mutating CLI calls — message stayed in
        # `processing` indefinitely (Apr 24 GR Little Extractor trace).
        did_useful_work, no_work_reason = agent_did_useful_work(messages)

        # Clean up causation env var
        os.environ.pop("INDEMN_CAUSATION_MESSAGE_ID", None)
        os.environ.pop("INDEMN_EFFECTIVE_ACTOR_ID", None)

        if did_useful_work:
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
        # Bug #3 fix: tear down the per-activity sandbox directory so /workspace
        # doesn't accumulate state across invocations on long-running runtimes.
        # Recomputed from input rather than relying on a closure so this runs
        # even if an early exception happened before activity_id was bound.
        activity_dir = f"/workspace/act-{input.message_id[:12]}"
        if os.path.exists(activity_dir):
            try:
                import shutil

                shutil.rmtree(activity_dir)
            except Exception as cleanup_error:  # noqa: BLE001
                log.warning(
                    "Failed to cleanup activity directory %s: %s",
                    activity_dir,
                    cleanup_error,
                )


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
        graceful_shutdown_timeout=timedelta(seconds=30),
    )

    log.info("Worker listening on queue: %s (max_concurrent=%d)", TASK_QUEUE, max_concurrent)

    await asyncio.gather(
        worker.run(),
        heartbeat_loop(interval_s=30.0),
    )


if __name__ == "__main__":
    asyncio.run(main())
