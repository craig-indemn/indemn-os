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

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.contrib.opentelemetry import TracingInterceptor

from indemn_os.types import AgentExecutionInput, AgentExecutionResult
from harness_common.cli import indemn, CLIError
from harness_common.runtime import RUNTIME_ID, register_instance, heartbeat_loop
from harness.agent import build_agent

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


@activity.defn
async def process_with_associate(input: AgentExecutionInput) -> AgentExecutionResult:
    """Agent execution loop. Migrated from kernel/temporal/activities.py.

    Harness orchestration uses the CLI for I/O (load context, mark complete).
    Agent's own tool execution uses deepagents' built-in execute via backend.
    """
    try:
        # Load associate config + context (harness orchestration, not agent tools)
        associate = indemn("associate", "get", input.associate_id)
        context = indemn(
            "entity", "get", input.entity_type, input.entity_id,
            "--depth", "2", "--include-related",
        )

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

        # Load skills — CLI verifies content_hash before returning [Q4]
        skill_contents = []
        for skill_ref in associate.get("skills", []):
            skill = indemn("skill", "get", skill_ref)
            skill_contents.append(skill["content"])

        # Build agent (thin — deepagents handles everything once backend is set)
        agent = build_agent(associate=associate, skills=skill_contents, llm_config=llm_config)

        # Heartbeat before the potentially long agent run
        activity.heartbeat("starting_agent")

        # Run the agent loop
        result = await agent.ainvoke({
            "messages": [{
                "role": "user",
                "content": f"Process this work:\n\n{context}",
            }],
        })

        # Mark the message complete — harness owns completion [Q1]
        indemn("queue", "complete", input.message_id)

        return AgentExecutionResult(
            status="complete",
            iterations=len(result.get("messages", [])),
            tools_used=[],
        )

    except Exception as e:
        # Mark the message failed — harness owns failure reporting [Q1]
        try:
            indemn("queue", "fail", input.message_id, "--reason", str(e)[:500])
        except CLIError:
            log.warning("Failed to report message failure via CLI: %s", e)
        raise  # Re-raise so Temporal marks the activity failed


async def main():
    log.info("Starting async-deepagents harness, runtime=%s", RUNTIME_ID)
    log.info("Sandbox type: %s", os.environ.get("INDEMN_SANDBOX_TYPE", "localshell"))

    await register_instance()

    client = await Client.connect(
        os.environ["TEMPORAL_ADDRESS"],
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )

    # Read capacity from Runtime config (not hardcoded)
    try:
        runtime_config = indemn("runtime", "get", RUNTIME_ID)
        max_concurrent = (
            runtime_config.get("capacity", {}).get("max_concurrent_sessions") or 10
        )
    except Exception:
        max_concurrent = 10  # Fallback if Runtime config unavailable at startup

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        activities=[process_with_associate],
        max_concurrent_activities=max_concurrent,
        interceptors=[TracingInterceptor()],
        graceful_shutdown_timeout=30,
    )

    log.info("Worker listening on queue: %s (max_concurrent=%d)", TASK_QUEUE, max_concurrent)

    await asyncio.gather(
        worker.run(),
        heartbeat_loop(interval_s=30.0),
    )


if __name__ == "__main__":
    asyncio.run(main())
