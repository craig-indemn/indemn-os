"""Per-connection session manager for the chat harness.

Manages the lifecycle of one WebSocket conversation:
- Interaction entity (conversation container)
- Attention entity (real-time session tracking)
- Agent instance (deepagents with checkpointer)
- Events stream (mid-conversation entity awareness)
- Heartbeat loop
- Handoff mode switching
"""

import asyncio
import json
import logging
import os
import subprocess

from starlette.websockets import WebSocket

from harness_common.attention import attention_heartbeat_loop, close_attention, open_attention
from harness_common.cli import CLIError, indemn
from harness_common.interaction import close_interaction, create_interaction
from harness_common.runtime import RUNTIME_ID
from harness.agent import build_agent

log = logging.getLogger(__name__)


def _merge_llm_config(runtime: dict, associate: dict, deployment: dict | None) -> dict:
    """Three-layer config merge per Phase 4-5 spec § 5.3."""
    return {
        **(runtime.get("llm_config") or {}),
        **(associate.get("llm_config") or {}),
        **((deployment.get("llm_override") or {}) if deployment else {}),
    }


class ChatSession:
    """Manages one WebSocket conversation session."""

    def __init__(self, websocket: WebSocket, associate_id: str, auth_token: str, checkpointer=None):
        self.ws = websocket
        self.associate_id = associate_id
        self.auth_token = auth_token
        self.checkpointer = checkpointer
        self.interaction_id = None
        self.attention_id = None
        self.agent = None
        self._heartbeat_task = None
        self._events_task = None
        self._events_process = None

    async def start(self):
        """Initialize the session — load config, create Interaction + Attention, build agent."""
        # Load associate config
        associate = indemn("actor", "get", self.associate_id)
        log.info("Loaded associate: %s (%s)", associate.get("name"), self.associate_id)

        # Load Runtime config for three-layer merge
        runtime = indemn("runtime", "get", RUNTIME_ID)

        # Load Deployment if present
        deployment = None
        deployment_id = associate.get("deployment_id")
        if deployment_id:
            try:
                deployment = indemn("deployment", "get", str(deployment_id))
            except CLIError:
                pass

        # Three-layer LLM config merge
        llm_config = _merge_llm_config(runtime, associate, deployment)

        # Load skills (CLI verifies hash integrity)
        skill_contents = []
        for skill_ref in associate.get("skills", []):
            skill = indemn("skill", "get", skill_ref)
            skill_contents.append(skill["content"])

        # Create Interaction entity
        interaction = await create_interaction(
            channel_type="chat",
            associate_id=self.associate_id,
            deployment_id=deployment_id,
        )
        self.interaction_id = interaction.get("_id")

        # Open Attention (real-time session tracking)
        attention = await open_attention(
            actor_id=self.associate_id,
            entity_type="Interaction",
            entity_id=self.interaction_id,
            purpose="real_time_session",
            runtime_id=RUNTIME_ID,
        )
        self.attention_id = attention.get("_id")

        # Build agent with checkpointer for conversation persistence
        self.agent = build_agent(
            associate=associate,
            skills=skill_contents,
            llm_config=llm_config,
            checkpointer=self.checkpointer,
        )

        # Start heartbeat loop
        self._heartbeat_task = asyncio.create_task(
            attention_heartbeat_loop(self.attention_id, interval_s=30.0)
        )

        # Start events stream for mid-conversation awareness
        self._events_task = asyncio.create_task(self._run_events_stream())

        log.info("Session started: interaction=%s, attention=%s",
                 self.interaction_id, self.attention_id)

    async def handle_message(self, content: str, context: dict | None = None):
        """Process one user message — run agent, stream response tokens."""
        if not self.agent:
            await self._send({"type": "error", "content": "Session not initialized"})
            return

        # Build the user message with context
        user_content = content
        if context:
            context_str = json.dumps(context, default=str)
            user_content = f"[UI Context: {context_str}]\n\n{content}"

        # Run agent — collect the response
        try:
            result = await self.agent.ainvoke(
                {"messages": [{"role": "user", "content": user_content}]},
                config={"configurable": {"thread_id": self.interaction_id}},
            )

            # Extract and send the agent's response
            messages = result.get("messages", [])
            for msg in messages:
                msg_type = getattr(msg, "type", type(msg).__name__)
                if msg_type == "ai":
                    content = getattr(msg, "content", "")
                    if content:
                        await self._send({"type": "response", "content": content})
                    # Send tool calls
                    for tc in getattr(msg, "tool_calls", []):
                        tc_name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                        tc_args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                        await self._send({"type": "tool_call", "name": tc_name, "args": tc_args})
                elif msg_type == "tool":
                    tool_name = getattr(msg, "name", "")
                    tool_content = getattr(msg, "content", "")
                    await self._send({"type": "tool_result", "name": tool_name, "content": str(tool_content)[:1000]})

            await self._send({"type": "done"})

        except Exception as e:
            log.error("Agent error: %s", e)
            await self._send({"type": "error", "content": str(e)[:500]})

    async def close(self):
        """Clean up session — close Attention, Interaction, stop background tasks."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._events_task:
            self._events_task.cancel()
        if self._events_process:
            self._events_process.terminate()

        if self.attention_id:
            await close_attention(self.attention_id)
        if self.interaction_id:
            await close_interaction(self.interaction_id)

        log.info("Session closed: interaction=%s", self.interaction_id)

    async def _send(self, data: dict):
        """Send a typed JSON message to the WebSocket client."""
        try:
            await self.ws.send_json(data)
        except Exception as e:
            log.warning("WebSocket send failed: %s", e)

    async def _run_events_stream(self):
        """Subscribe to mid-conversation entity events via `indemn events stream`."""
        if not self.interaction_id:
            return

        try:
            env = {
                "INDEMN_API_URL": os.environ["INDEMN_API_URL"],
                "INDEMN_SERVICE_TOKEN": os.environ.get("INDEMN_SERVICE_TOKEN", self.auth_token),
                "PATH": os.environ["PATH"],
            }
            self._events_process = subprocess.Popen(
                ["indemn", "events", "stream",
                 "--actor", self.associate_id,
                 "--interaction", self.interaction_id,
                 "--format", "json-lines"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            loop = asyncio.get_event_loop()
            while True:
                line = await loop.run_in_executor(None, self._events_process.stdout.readline)
                if not line:
                    break
                try:
                    event = json.loads(line.decode())
                    await self._send({"type": "event", "data": event})
                    # TODO: feed event to running agent for mid-conversation awareness
                except json.JSONDecodeError:
                    pass

        except asyncio.CancelledError:
            if self._events_process:
                self._events_process.terminate()
        except Exception as e:
            log.warning("Events stream error: %s", e)
