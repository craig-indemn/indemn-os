"""Per-connection session manager for the chat harness.

Manages the lifecycle of one WebSocket conversation:
- Interaction entity (conversation container)
- Attention entity (real-time session tracking)
- Agent instance (deepagents with checkpointer)
- Events stream (mid-conversation entity awareness)
- Heartbeat loop
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
from langchain_core.messages import SystemMessage
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

    @staticmethod
    def compose_initial_messages(
        skill_content: str, deployment_context: dict
    ) -> list:
        """Compose the <skill> + <deployment_context> SystemMessages prepended
        at chat session start (AI-407 §15.5 chat).

        Phase 4 chat shape: the agent's DEFAULT_PROMPT tells the agent to
        "Read your <skill> SystemMessage" + "Read <deployment_context>
        SystemMessage". This function produces both. The caller (start()
        for new sessions) prepends them to the agent's checkpointer state
        keyed by interaction_id; subsequent turns see them in conversation
        history without re-prepending. Resumed sessions inherit the
        prior-session state directly.

        deployment_context is a dict the agent reads to know who the user
        is, what page they're on, what scope this session has. Sanitization
        of user-controlled values is the caller's job (sanitize_dynamic_params
        from harness_common.sanitize — Task 2.0.5).
        """
        ctx_lines = "\n".join(f"  {k}: {v}" for k, v in deployment_context.items())
        return [
            SystemMessage(content=f"<skill>\n{skill_content}\n</skill>"),
            SystemMessage(
                content=(
                    f"<deployment_context>\n{ctx_lines}\n</deployment_context>\n\n"
                    "Read this block before responding. It tells you who the "
                    "user is and what context this session has."
                )
            ),
        ]

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

        # Build agent — skills load via CLI directives in the system prompt
        # (commit `7281b83` pattern), no filesystem skill writing.
        self.agent = build_agent(
            associate=associate,
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
            "Session started: interaction=%s, attention=%s",
            self.interaction_id,
            self.attention_id,
        )

    async def handle_message(self, content: str, context: dict | None = None):
        """Process one user message — stream response tokens to the UI."""
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

        # Stream agent response — tokens arrive as they're generated
        try:
            async for event in self.agent.astream_events(
                {"messages": messages},
                config={"configurable": {"thread_id": self.interaction_id}},
                version="v2",
            ):
                kind = event.get("event", "")

                if kind == "on_chat_model_stream":
                    # Token-by-token streaming from the LLM
                    chunk = event.get("data", {}).get("chunk")
                    if chunk:
                        token = getattr(chunk, "content", "")
                        if token:
                            await self._send({"type": "token", "content": token})

                        # Tool calls within streaming chunks
                        tool_call_chunks = getattr(chunk, "tool_call_chunks", [])
                        for tc in tool_call_chunks:
                            if tc.get("name"):
                                await self._send(
                                    {
                                        "type": "tool_call",
                                        "name": tc.get("name", ""),
                                        "args": tc.get("args", {}),
                                        "call_id": tc.get("id", ""),
                                    }
                                )

                elif kind == "on_tool_end":
                    # Send entity data to UI — the LLM may summarize instead
                    # of echoing JSON, so we must detect here.
                    # Client-side detection handles the case where LLM echoes JSON.
                    output = event.get("data", {}).get("output", "")
                    if hasattr(output, "content"):
                        content_str = output.content
                    elif isinstance(output, dict) and "content" in output:
                        content_str = output["content"]
                    else:
                        content_str = str(output)
                    if not isinstance(content_str, str):
                        content_str = str(content_str)
                    await self._classify_and_send_tool_result(event.get("name", ""), content_str)

            await self._send({"type": "done"})

        except Exception as e:
            log.error("Agent error: %s", e, exc_info=True)
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

        log.info(
            "Classifying tool result: name=%s, first_200=%r",
            tool_name,
            content_str[:200],
        )

        # Try to parse as JSON for entity detection.
        # The execute tool may return output with extra text — try to find JSON within.
        json_str = content_str.strip()

        # Try to parse JSON — CLI may append decorative borders after the data.
        data = None
        try:
            data = json.loads(json_str)
        except (json.JSONDecodeError, TypeError):
            # Find the JSON portion — look for matching brackets
            if json_str.startswith("["):
                # Find closing ] by parsing incrementally
                depth = 0
                for i, ch in enumerate(json_str):
                    if ch == "[":
                        depth += 1
                    elif ch == "]":
                        depth -= 1
                        if depth == 0:
                            try:
                                data = json.loads(json_str[: i + 1])
                            except (json.JSONDecodeError, TypeError):
                                pass
                            break
            elif json_str.startswith("{"):
                depth = 0
                for i, ch in enumerate(json_str):
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                data = json.loads(json_str[: i + 1])
                            except (json.JSONDecodeError, TypeError):
                                pass
                            break

        if data is None:
            log.info("No JSON detected, sending as tool_result (len=%d)", len(content_str))
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
            log.info("Detected entity_list: %d items", len(data))
            await self._send({"type": "entity_list", "data": data, "entity_type": ""})
            return

        # Detect single entity (dict with _id)
        if isinstance(data, dict) and "_id" in data:
            log.info("Detected entity_detail: %s", data.get("name", data.get("_id")))
            await self._send({"type": "entity_detail", "data": data, "entity_type": ""})
            return

        # Fallback: parsed JSON but not entity data
        log.info("Tool result is JSON but not entity data, type=%s", type(data).__name__)
        await self._send({"type": "tool_result", "name": tool_name, "content": content_str[:1000]})

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
