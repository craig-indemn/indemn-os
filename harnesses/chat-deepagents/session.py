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

from harness.agent import build_agent
from harness_common.attention import attention_heartbeat_loop, close_attention, open_attention
from harness_common.cli import CLIError, indemn
from harness_common.interaction import close_interaction, create_interaction
from harness_common.runtime import RUNTIME_ID
from starlette.websockets import WebSocket

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

    def __init__(
        self,
        websocket: WebSocket,
        associate_id: str,
        auth_token: str,
        checkpointer=None,
        interaction_id=None,
    ):
        self.ws = websocket
        self.associate_id = associate_id
        self.auth_token = auth_token
        self.checkpointer = checkpointer
        self.interaction_id = interaction_id
        self.attention_id = None
        self.agent = None
        self._heartbeat_task = None
        self._events_task = None
        self._events_process = None
        self._event_queue: list[dict] = []
        self._message_count = 0

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

        # Load all skills in one CLI call and filter to associate's skill refs
        skill_refs = associate.get("skills", [])
        if skill_refs:
            loop = asyncio.get_event_loop()
            all_skills = await loop.run_in_executor(
                None, indemn, "skill", "list", "--format", "json"
            )
            skill_map = {s["name"]: s["content"] for s in all_skills}
            skill_contents = [skill_map[ref] for ref in skill_refs if ref in skill_map]
            if len(skill_contents) < len(skill_refs):
                missing = [r for r in skill_refs if r not in skill_map]
                log.warning("Skills not found: %s", missing)
        else:
            skill_contents = []

        # Resume existing Interaction or create a new one
        if self.interaction_id:
            log.info("Resuming interaction: %s", self.interaction_id)
        else:
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

        log.info(
            "Session started: interaction=%s, attention=%s", self.interaction_id, self.attention_id
        )

    async def handle_message(self, content: str, context: dict | None = None):
        """Process one user message — run agent, stream response tokens."""
        if not self.agent:
            await self._send({"type": "error", "content": "Session not initialized"})
            return

        # Save first message preview for conversation history UI
        if self._message_count == 0 and self.interaction_id:
            try:
                indemn(
                    "interaction",
                    "update",
                    self.interaction_id,
                    "--data",
                    json.dumps({"first_message_preview": content[:100]}),
                )
            except Exception:
                pass  # non-critical
        self._message_count += 1

        # Build the user message with context
        user_content = content
        if context:
            context_str = json.dumps(context, default=str)
            user_content = f"[UI Context: {context_str}]\n\n{content}"

        # Drain queued events into a system message for mid-conversation awareness
        messages = []
        if self._event_queue:
            events = self._event_queue.copy()
            self._event_queue.clear()
            event_summaries = []
            for ev in events:
                entity_type = ev.get("entity_type", "unknown")
                entity_id = ev.get("entity_id", "unknown")
                event_type = ev.get("event_type", "change")
                event_summaries.append(f"{entity_type}/{entity_id}: {event_type}")
            system_event_msg = "[System events since last turn: " + "; ".join(event_summaries) + "]"
            messages.append({"role": "system", "content": system_event_msg})
        messages.append({"role": "user", "content": user_content})

        # Run agent — collect the response
        try:
            result = await self.agent.ainvoke(
                {"messages": messages},
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
                        tc_name = (
                            tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                        )
                        tc_args = (
                            tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                        )
                        tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                        await self._send(
                            {
                                "type": "tool_call",
                                "name": tc_name,
                                "args": tc_args,
                                "call_id": tc_id,
                            }
                        )
                elif msg_type == "tool":
                    tool_name = getattr(msg, "name", "")
                    tool_content = getattr(msg, "content", "")
                    await self._classify_and_send_tool_result(tool_name, tool_content)

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

    async def _classify_and_send_tool_result(self, tool_name: str, tool_content):
        """Classify tool output — detect entity data and send as typed message."""
        content_str = str(tool_content) if not isinstance(tool_content, str) else tool_content

        # Try to parse as JSON for entity detection
        try:
            data = json.loads(content_str)
        except (json.JSONDecodeError, TypeError):
            await self._send(
                {"type": "tool_result", "name": tool_name, "content": content_str[:1000]}
            )
            return

        # Detect entity list (array of dicts with _id)
        if (
            isinstance(data, list)
            and len(data) > 0
            and isinstance(data[0], dict)
            and "_id" in data[0]
        ):
            entity_type = self._infer_entity_type(tool_name)
            await self._send({"type": "entity_list", "data": data, "entity_type": entity_type})
            return

        # Detect single entity (dict with _id)
        if isinstance(data, dict) and "_id" in data:
            entity_type = self._infer_entity_type(tool_name)
            await self._send({"type": "entity_detail", "data": data, "entity_type": entity_type})
            return

        # Fallback: send as tool_result
        await self._send({"type": "tool_result", "name": tool_name, "content": content_str[:1000]})

    def _infer_entity_type(self, tool_name: str) -> str:
        """Infer entity type from the tool/command name.

        deepagents execute tool captures the full command.
        Pattern: 'indemn <entity_type> list/get/create/...'
        """
        # tool_name from deepagents is typically "execute" — the command is in args
        # But the content itself reveals the entity type from the data shape
        # For now, return empty and let the UI infer from data
        return ""

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
                [
                    "indemn",
                    "events",
                    "stream",
                    "--actor",
                    self.associate_id,
                    "--interaction",
                    self.interaction_id,
                ],
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
                    self._event_queue.append(event)
                except json.JSONDecodeError:
                    pass

        except asyncio.CancelledError:
            if self._events_process:
                self._events_process.terminate()
        except Exception as e:
            log.warning("Events stream error: %s", e)
