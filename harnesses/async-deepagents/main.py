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


def _write_skills_to_filesystem(skill_refs: list[str], activity_id: str) -> str | None:
    """Fetch associate skills and write them as proper deepagents skills under
    a per-activity skills library directory. Returns the LIBRARY DIRECTORY path
    (deepagents discovers skills by scanning the library for subdirectories
    with a SKILL.md inside), or None if no skills were written.

    Layout — required by deepagents:
        /workspace/{activity_id}/skills/                        ← library dir (passed to deepagents)
                                       /{skill-slug}/SKILL.md   ← individual skill

    Apr 28 finding (Diana@CKSpecialty trace 019dd5f3-…): the previous version
    of this function passed each individual skill subdirectory to deepagents
    instead of the parent library dir. deepagents interpreted the per-skill
    path as a library and looked inside it for skill subdirs, found none, and
    surfaced "(No skills available yet. You can create skills in <path>)" in
    the agent's system prompt — meaning the agent never loaded the associate
    skill content. Every iteration of email-classifier (v3 → v7) was thus
    ineffective: the agent only ever followed the harness DEFAULT_PROMPT.

    Fix: pass the parent library dir, not the per-skill dir.

    Only writes associate-type skills (behavioral instructions). Entity skills
    are NOT pre-loaded — the agent reads them on demand via
    execute("indemn skill get <EntityName>").
    """
    if not skill_refs:
        return None

    # Per-activity library dir — this is what gets passed to deepagents.
    skills_lib_dir = f"/workspace/{activity_id}/skills"
    os.makedirs(skills_lib_dir, exist_ok=True)

    written = 0
    for ref in skill_refs:
        try:
            skill = indemn("skill", "get", ref)
        except CLIError:
            log.warning("Skill not found: %s", ref)
            continue

        if skill.get("type") == "entity":
            continue

        slug = ref.lower().replace(" ", "-")
        skill_dir = os.path.join(skills_lib_dir, slug)
        os.makedirs(skill_dir, exist_ok=True)

        content = skill.get("content", "")
        name = skill.get("name", ref)
        description = skill.get("description", f"Skill: {name}")

        skill_file = os.path.join(skill_dir, "SKILL.md")
        with open(skill_file, "w") as f:
            f.write(f"---\nname: {name}\ndescription: {description}\n---\n\n")
            f.write(content)
        written += 1

    if written == 0:
        return None

    log.info(
        "Wrote %d associate skill(s) to library %s for deepagents to discover",
        written,
        skills_lib_dir,
    )
    # Return the LIBRARY dir as an ABSOLUTE path. deepagents' FilesystemBackend
    # (parent of LocalShellBackend) resolves relative paths against root_dir,
    # which is also /workspace/{activity_id} — so a relative "{activity_id}/skills"
    # would double-nest to /workspace/{activity_id}/{activity_id}/skills and
    # find nothing (Bug #35, confirmed via deepagents 0.5.3 source +
    # repro on 2026-04-29). Absolute paths bypass cwd resolution.
    return skills_lib_dir


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
        # Dynamic entity instances with related entities per design (depth 2)
        entity_slug = input.entity_type.lower()
        context = indemn(entity_slug, "get", input.entity_id, "--depth", "2", "--include-related")

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

        # Write associate skill(s) to the per-activity skills library dir for
        # deepagents progressive disclosure. Returns the library dir path (NOT
        # per-skill subdir paths — see _write_skills_to_filesystem docstring
        # for the Apr 28 fix). Entity skills are NOT pre-loaded — the agent
        # reads them on demand via execute("indemn skill get <EntityName>").
        activity_id = f"act-{input.message_id[:12]}"
        skills_lib_dir = _write_skills_to_filesystem(associate.get("skills", []), activity_id)

        # Build agent (thin — deepagents handles everything once backend is set).
        # Bug #3 fix: pass activity_id so the sandbox root_dir is scoped per
        # activity, preventing cross-invocation tool-cache leaks where one
        # agent's grep matched another agent's cached results.
        agent = build_agent(
            associate=associate,
            skills_lib_dir=skills_lib_dir,
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
