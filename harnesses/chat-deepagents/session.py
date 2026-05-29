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
from harness_common.sanitize import sanitize_dynamic_params
from harness_common.thread_id import derive_checkpointer_thread_id
from langchain_core.messages import SystemMessage
from starlette.websockets import WebSocket
from types import SimpleNamespace
import uuid

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

    def _session_indemn(self, *args):
        """Per-session indemn() wrapper — passes session-local correlation_id
        + effective_actor_id as kwargs (AI-407 Task 2.11 + Task 2.5).

        Chat is multi-session-per-process. Mutating os.environ to set
        INDEMN_CORRELATION_ID at session start would race with concurrent
        sessions and contaminate cross-session lineage attribution. Per-call
        kwargs (added on the wrapper by Task 2.5) are the concurrency-safe
        path. Early-lifecycle calls (before self.correlation_id is set)
        safely pass None — the wrapper's `if correlation_id is not None`
        branch skips the env-setting.

        effective_actor_id reads from self.effective_actor_id (AI-408 Phase 3).
        Two cases flow through this single attribute:
        - **Legacy associate_id-only path:** ChatSession constructed without
          the AI-408 kwargs; __init__'s `effective_actor_id or associate_id`
          default makes self.effective_actor_id == self.associate_id, so
          forensics attribution is unchanged from pre-AI-408 ("the chat
          associate acted").
        - **Deployment-driven (AI-408 Task 3.5):** _start_deployment_session
          computes effective_actor_id from the acts_as gate — JWT.sub for
          session_actor (the impersonated user), Deployment.associate_id
          for associate_self (the agent acting as itself) — and passes it
          to ChatSession.__init__.

        The load-bearing invariant lives at _start_deployment_session's
        `effective_actor_id = authenticated_actor_id` line + this attribute
        read; neither reads from dynamic_params.
        """
        return indemn(
            *args,
            correlation_id=self.correlation_id,
            effective_actor_id=self.effective_actor_id,
        )

    @staticmethod
    def build_runnable_config(
        *,
        interaction_id: str,
        correlation_id: str,
        associate: dict,
        runtime_id: str,
        deployment_id: str | None,
    ) -> dict:
        """RunnableConfig per AI-407 §13.5 (chat — real-time).

        - configurable.thread_id = interaction_id (checkpointer state continuity
          across turns; matches Phase 3 keying so no checkpoint invalidation).
        - metadata.thread_id = correlation_id (LangSmith UI groups runs by
          cascade lineage; distinct from the checkpointer key per §13.2).
        - metadata carries the full ID set for cross-pivot LangSmith search.
        """
        work_ctx = SimpleNamespace(
            is_real_time_session=True,
            interaction_id=interaction_id,
            target_entity_type=None,
            target_entity_id=None,
            message_id=None,
        )
        checkpointer_thread_id = derive_checkpointer_thread_id(work_ctx)
        associate_name = associate.get("name", "Chat Assistant")
        return {
            "configurable": {"thread_id": str(checkpointer_thread_id)},
            "metadata": {
                "thread_id": correlation_id,  # LangSmith UI grouping
                "correlation_id": correlation_id,
                "interaction_id": interaction_id,
                "associate_id": str(associate.get("_id", "")),
                "associate_name": associate_name,
                "entity_type": "Interaction",
                "entity_id": interaction_id,
                "runtime_id": str(runtime_id),
                "deployment_id": deployment_id,
            },
            "tags": [
                f"associate:{associate_name}",
                "channel:chat",
                f"runtime:{runtime_id}",
                (f"deployment:{deployment_id}" if deployment_id else "deployment:none"),
            ],
            "run_name": f"{associate_name} → Interaction {interaction_id[:8]}",
        }

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
        deployment: dict | None = None,
        dynamic_params: dict | None = None,
        effective_actor_id: str | None = None,
        validation_warnings: list[str] | None = None,
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
        # AI-407 Phase 4: tracked at session start; consumed by build_runnable_config + compose_initial_messages
        self.correlation_id: str | None = None
        self.runtime_id: str = RUNTIME_ID
        self.deployment_id: str | None = None
        self.associate: dict | None = None
        self._initial_systemmessages: list | None = None
        # AI-408 Phase 3: Deployment-driven session shape. When `deployment`
        # is None we're on the legacy associate_id-only path (current OS
        # Base UI). When set, start() uses the supplied Deployment instead
        # of the one referenced from associate.deployment_id; dynamic_params
        # + effective_actor_id flow into build_runnable_config + the
        # <deployment_context> SystemMessage (Task 3.7).
        self.deployment: dict | None = deployment
        self.dynamic_params: dict = dynamic_params or {}
        # effective_actor_id is the identity attributed to entity writes
        # this session makes. Defaults to associate_id (legacy + pre-Task-3.5
        # state). Task 3.5 overrides this with JWT.sub for session_actor
        # Deployments. _session_indemn reads this attribute on every CLI
        # call (kept in sync with the AI-407 per-call kwarg path).
        self.effective_actor_id: str = effective_actor_id or associate_id
        # AI-408 Task 3.6 follow-up: forgiving-mode parameter_schema warnings
        # surfaced to the client in the `connected` payload (per plan §3.6:
        # "If forgiving → continue with validation_warnings in the connected
        # response"). Empty list when no warnings — kept as a stable field
        # shape so SDKs can iterate without null-checking. Legacy (no
        # deployment) path always passes [] since there's no schema to
        # validate against.
        self.validation_warnings: list[str] = validation_warnings or []

    async def start(self):
        """Initialize the session — load config, create Interaction + Attention, build agent."""
        # Load associate config (correlation_id not yet set — that's OK; _session_indemn
        # handles None by skipping the per-call env override)
        associate = self._session_indemn("actor", "get", self.associate_id)
        log.info("Loaded associate: %s (%s)", associate.get("name"), self.associate_id)
        self.associate = associate

        # Load Runtime config for three-layer merge
        runtime = self._session_indemn("runtime", "get", RUNTIME_ID)

        # Load Deployment if present
        deployment = None
        deployment_id = associate.get("deployment_id")
        if deployment_id:
            try:
                deployment = self._session_indemn("deployment", "get", str(deployment_id))
            except CLIError:
                pass
        self.deployment_id = str(deployment_id) if deployment_id else None

        # Three-layer LLM config merge
        llm_config = _merge_llm_config(runtime, associate, deployment)

        # Resume existing Interaction or create a new one
        is_resume = bool(self.interaction_id)
        if is_resume:
            log.info("Resuming interaction: %s", self.interaction_id)
            # Try to recover correlation_id from existing Interaction (Phase 4
            # tracks lineage on the conversation entity). Pre-Phase-4 Interactions
            # may have no correlation_id — fall back to fresh UUID.
            try:
                interaction = self._session_indemn("interaction", "get", self.interaction_id)
                recovered = interaction.get("correlation_id")
                if recovered:
                    self.correlation_id = recovered
                else:
                    # Pre-Phase-4 Interaction lacks correlation_id, OR a
                    # Phase-4 Interaction that should have one is missing it
                    # (suggests an AI-407 regression on the cascade-lineage
                    # write path). Either way we mint a fresh UUID + log a
                    # WARNING so this surfaces in monitoring instead of
                    # silently dropping the resume's lineage attribution.
                    log.warning(
                        "Resume Interaction %s missing correlation_id; "
                        "minting fresh UUID (lineage continuity lost)",
                        self.interaction_id,
                    )
                    self.correlation_id = str(uuid.uuid4())
            except Exception:
                # CLI failure / Interaction not found / etc. — log + fall
                # through to fresh UUID so resume still proceeds with
                # best-effort continuity. The session works; lineage may
                # be reset.
                log.warning(
                    "Failed to recover correlation_id from resume "
                    "Interaction %s; minting fresh UUID",
                    self.interaction_id,
                    exc_info=True,
                )
                self.correlation_id = str(uuid.uuid4())
        else:
            interaction = await create_interaction(
                channel_type="chat",
                associate_id=self.associate_id,
                deployment_id=deployment_id,
            )
            self.interaction_id = interaction.get("_id")
            # AI-407 §13.5: chat sessions generate a fresh correlation_id at start.
            # AI-408 will let the connect message pass one in for cross-channel chains.
            self.correlation_id = str(uuid.uuid4())

        # Open Attention (real-time session tracking)
        attention = await open_attention(
            actor_id=self.associate_id,
            entity_type="Interaction",
            entity_id=self.interaction_id,
            purpose="real_time_session",
            runtime_id=RUNTIME_ID,
        )
        self.attention_id = attention.get("_id")

        # Build agent — operating skill arrives as <skill> SystemMessage at
        # session start (Phase 4 — Task 2.9 + this task). Entity skills still
        # via CLI directives in DEFAULT_PROMPT (Step 3).
        self.agent = build_agent(
            associate=associate,
            llm_config=llm_config,
            checkpointer=self.checkpointer,
        )

        # AI-407 Phase 4: compose initial <skill> + <deployment_context>
        # SystemMessages for NEW sessions. They're prepended to the per-turn
        # messages array on the first turn only (handle_message); the MongoDB
        # checkpointer persists them in state via the add_messages reducer,
        # so subsequent turns see them in history without re-prepending.
        # Resumed sessions inherit the prior state directly (no re-prepend).
        if not is_resume:
            skill_xml = self._load_skill_section_xml(associate)
            deployment_context = self._build_deployment_context(associate, deployment)
            self._initial_systemmessages = ChatSession.compose_initial_messages(
                skill_xml, deployment_context
            )

        # Start heartbeat loop
        self._heartbeat_task = asyncio.create_task(
            attention_heartbeat_loop(self.attention_id, interval_s=30.0)
        )

        # Start events stream for mid-conversation awareness
        self._events_task = asyncio.create_task(self._run_events_stream())

        log.info(
            "Session started: interaction=%s, attention=%s, correlation=%s",
            self.interaction_id,
            self.attention_id,
            self.correlation_id,
        )

    def _load_skill_section_xml(self, associate: dict) -> str:
        """Load operating skill(s) content via CLI, format as nested
        <skill name="X">...</skill> blocks. compose_initial_messages wraps
        the result in an outer <skill>...</skill> for the SystemMessage.

        Mirrors async-deepagents' _build_skill_section_xml. Pulled inline
        rather than DRY'd into harness_common to keep Phase 2A's harness-
        common touch surface minimal (only adds the new utility modules +
        cli.py kwargs).
        """
        parts: list[str] = []
        for ref in associate.get("skills") or []:
            try:
                skill = self._session_indemn("skill", "get", ref)
                content = skill.get("content", "") if isinstance(skill, dict) else str(skill)
                parts.append(f'<skill name="{ref}">')
                parts.append(content)
                parts.append("</skill>")
                parts.append("")
            except CLIError as e:
                log.warning("Failed to load skill %s: %s", ref, e)
        return "\n".join(parts).rstrip()

    def _build_deployment_context(
        self, associate: dict, deployment: dict | None
    ) -> dict:
        """Build the deployment_context dict for the <deployment_context>
        SystemMessage (AI-408 Task 3.7).

        Three layers, applied in order (later overrides earlier):

        1. **Operator-trusted static_parameters** (lowest priority) — set on
           the Deployment record by the operator. Trusted, no sanitization.

        2. **User-supplied dynamic_params** (sanitized via
           sanitize_dynamic_params per §10.7 layer-c — strips newlines /
           HTML tags / caps length, defending against pseudo-SystemMessage
           injection like `"\\n[NEW INSTRUCTION]..."`). Overrides static
           for any operator-suppliable key (e.g., user-set `current_route`
           overrides any operator-default).

        3. **Security-determined fields** (highest priority — applied LAST
           so user-supplied params can NEVER spoof them):
           - `actor_id` = `self.effective_actor_id` (from Task 3.5's
             acts_as gate: JWT.sub for session_actor, Deployment.associate_id
             for associate_self). A dynamic_params.actor_id that named a
             different user would be silently overridden here even if it
             survived schema validation + the acts_as mismatch check.
           - `channel_kind` = "chat" (the surface this session is on)
           - `deployment_id` / `deployment_name` = from the Deployment record

        Legacy (no deployment) path: skips layers 1+2, only security-
        determined fields appear. effective_actor_id defaults to
        associate_id in __init__, so behavior matches pre-AI-408.
        """
        ctx: dict = {}
        if deployment:
            # Layer 1: operator-trusted static params
            ctx.update(deployment.get("static_parameters") or {})
            # Layer 2: user-supplied dynamic params (sanitized — §10.7 layer-c)
            ctx.update(sanitize_dynamic_params(self.dynamic_params or {}))
        # Layer 3: security-determined fields (override anything user-supplied)
        ctx["actor_id"] = str(self.effective_actor_id)
        ctx["actor_name"] = associate.get("name", "Assistant")
        ctx["channel_kind"] = "chat"
        if deployment:
            ctx["deployment_id"] = str(deployment.get("_id", ""))
            ctx["deployment_name"] = deployment.get("name", "")
        return ctx

    async def handle_message(self, content: str, context: dict | None = None):
        """Process one user message — stream response tokens to the UI."""
        if not self.agent:
            await self._send({"type": "error", "content": "Session not initialized"})
            return

        # Save first message preview for conversation history UI
        if self._message_count == 0 and self.interaction_id:
            try:
                self._session_indemn(
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

        # AI-407 Phase 4: prepend initial <skill> + <deployment_context>
        # SystemMessages on FIRST turn of new sessions. Set by start() for
        # non-resume sessions; cleared after first use so we don't re-prepend.
        # Persists in checkpointer state via add_messages reducer.
        if self._initial_systemmessages:
            initial = [
                {"role": "system", "content": m.content}
                for m in self._initial_systemmessages
            ]
            messages = initial + messages
            self._initial_systemmessages = None

        messages.append({"role": "user", "content": user_content})

        # Stream agent response — tokens arrive as they're generated.
        # Per-turn RunnableConfig per AI-407 §13.5: configurable.thread_id =
        # interaction_id (state across turns); metadata.thread_id = correlation_id
        # (LangSmith UI grouping); plus full §13 metadata block for cross-pivot.
        runnable_config = ChatSession.build_runnable_config(
            interaction_id=self.interaction_id,
            correlation_id=self.correlation_id,
            associate=self.associate or {},
            runtime_id=self.runtime_id,
            deployment_id=self.deployment_id,
        )
        # Match async-deepagents recursion budget (200 steps). LangGraph's
        # default of 25 is too low for real customer turns where the agent
        # needs multi-step tool exploration (`indemn company entity-resolve`
        # → `indemn deal list` → `indemn proposal get` chains). Surfaced
        # live 2026-05-29 chat smoke: agent hit recursion=25 mid-Alliance
        # lookup. See async main.py:823 for the canonical pattern.
        runnable_config["recursion_limit"] = 200
        try:
            async for event in self.agent.astream_events(
                {"messages": messages},
                config=runnable_config,
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
