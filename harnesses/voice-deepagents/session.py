"""Per-room session manager for the voice harness.

Mirrors `harnesses/chat-deepagents/session.py::ChatSession` — the OS
lifecycle (Interaction + Attention + heartbeat + events stream + agent
build) is identical; only the I/O transport differs. Chat speaks
WebSocket; voice speaks LiveKit's AgentSession (STT -> LLM -> TTS).

Per-call lifecycle:
1. JobContext arrives (one per LiveKit room a user joins)
2. Load associate config (CLI: indemn actor get)
3. Load runtime + deployment for three-layer LLM config merge
4. Write skills to filesystem for deepagents progressive disclosure
5. Create Interaction entity (channel_type=voice)
6. Open Attention (purpose=real_time_session, runtime_id, session_id)
7. Build deepagents agent (same agent code as chat + async; voice prompt)
8. Wrap agent in DeepagentsLLM adapter (LiveKit-compatible)
9. Start heartbeat loop (30s)
10. Start events stream subprocess for mid-conversation entity awareness
11. Construct LiveKit AgentSession with STT/LLM/TTS/VAD + DeepagentsLLM
12. Greet the user and run the conversation until the room closes
13. On close: cancel tasks, close Attention + Interaction, kill events
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from typing import Any

from harness.agent import build_agent
from harness.llm_adapter import DeepagentsLLM
from harness_common.attention import attention_heartbeat_loop, close_attention, open_attention
from harness_common.cli import CLIError, indemn
from harness_common.interaction import close_interaction
from harness_common.runtime import RUNTIME_ID
from harness_common.sanitize import sanitize_dynamic_params
from harness_common.thread_id import derive_checkpointer_thread_id
from langchain_core.messages import AIMessage, SystemMessage

log = logging.getLogger(__name__)


def _merge_llm_config(runtime: dict, associate: dict, deployment: dict | None) -> dict:
    """Three-layer config merge per Phase 4-5 spec § 5.3.

    Identical to chat-deepagents/session.py::_merge_llm_config.
    """
    return {
        **(runtime.get("llm_config") or {}),
        **(associate.get("llm_config") or {}),
        **((deployment.get("llm_override") or {}) if deployment else {}),
    }


class VoiceSession:
    """Manages one LiveKit room's voice conversation session."""

    @staticmethod
    def _resolve_effective_actor_id(
        deployment: dict, dynamic_params: dict, associate_id: str
    ) -> str:
        """Resolve INDEMN_EFFECTIVE_ACTOR_ID per Deployment.acts_as (§5.6).

        - `session_actor` → dynamic_params["actor_id"] (the user driving the
          conversation, validated upstream by frontdoor's JWT check —
          §10.3.1 step 7 ensures JWT.sub == dynamic_params.actor_id when
          acts_as=session_actor)
        - `associate_self` (or missing/unknown) → associate_id (the agent
          acts as itself, with its own role permissions)

        Defensive fallback: if acts_as=session_actor but actor_id is missing
        from dynamic_params (shouldn't happen if /sessions parameter_schema
        validation worked, but be safe), fall back to associate_id. The
        operator can detect this via runtime logs.
        """
        acts_as = deployment.get("acts_as", "associate_self")
        if acts_as == "session_actor":
            user_actor_id = dynamic_params.get("actor_id")
            if user_actor_id:
                return user_actor_id
            log.warning(
                "Deployment.acts_as=session_actor but dynamic_params.actor_id "
                "is missing — falling back to associate_id (operator should "
                "investigate /sessions parameter_schema)"
            )
            return associate_id
        # associate_self or unknown
        return associate_id

    def _session_indemn(self, *args):
        """Per-session indemn() wrapper — passes session-local correlation_id
        + effective_actor_id as kwargs (AI-407 §5.6 + §13.7).

        Voice is single-session-per-process today, but using per-call kwargs
        (vs os.environ mutation) keeps the harness contract uniform across
        all 3 harnesses (async + chat + voice) AND insulates against future
        LiveKit-Agents changes to multi-room-per-worker.

        effective_actor_id is computed once in start() via
        _resolve_effective_actor_id and stored on self._effective_actor_id.
        Early-lifecycle calls (before correlation_id is set from
        room.metadata) pass None — the wrapper's None-safe branch skips
        the per-call env override.
        """
        return indemn(
            *args,
            correlation_id=self.correlation_id,
            effective_actor_id=self._effective_actor_id,
        )

    @staticmethod
    def build_runnable_config(
        *,
        interaction_id: str,
        correlation_id: str,
        associate: dict,
        runtime_id: str,
        deployment_id: str | None,
        channel_kind: str = "voice",
    ) -> dict:
        """RunnableConfig per AI-407 §13.5 (voice — real-time).

        - configurable.thread_id = interaction_id (checkpointer state continuity
          across turns within a session; MongoDB checkpointer key per §13.3).
        - metadata.thread_id = correlation_id (LangSmith UI groups runs by
          cascade lineage; distinct from the checkpointer key per §13.2).
        - metadata carries the full ID set for cross-pivot LangSmith search
          (correlation_id, interaction_id, associate_id, associate_name,
          entity_type=Interaction, entity_id=interaction_id, runtime_id,
          deployment_id).

        Pre-fix the voice harness set ONLY configurable.thread_id and let
        LangSmith infer thread from that as a fallback — which conflates the
        observability key with the checkpointer state key. §13.5: set both
        explicitly + distinctly.
        """
        from types import SimpleNamespace

        work_ctx = SimpleNamespace(
            is_real_time_session=True,
            interaction_id=interaction_id,
            target_entity_type=None,
            target_entity_id=None,
            message_id=None,
        )
        # derive_checkpointer_thread_id imported at module top (real harness_common)
        checkpointer_thread_id = derive_checkpointer_thread_id(work_ctx)
        associate_name = associate.get("name", "Voice Assistant")
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
                f"channel:{channel_kind}",
                f"runtime:{runtime_id}",
                (
                    f"deployment:{deployment_id}"
                    if deployment_id
                    else "deployment:none"
                ),
            ],
            "run_name": f"{associate_name} → Interaction {interaction_id[:8]}",
        }

    @staticmethod
    def compose_initial_messages(
        skill_content: str, merged_context: dict
    ) -> list:
        """Compose the <skill> + <deployment_context> SystemMessages prepended
        at voice session start (AI-407 §15.5 voice).

        Phase 4 voice shape: the agent's DEFAULT_PROMPT tells the agent to
        "Read your <skill> SystemMessage" + "Read <deployment_context>
        SystemMessage". This function produces both. The caller (start())
        stores them on self._initial_systemmessages; the DeepagentsLLM
        adapter prepends them to the first turn's messages array. The
        MongoDB checkpointer keyed by interaction_id (§13.3) persists them
        as part of state via the add_messages reducer.

        merged_context is the result of merging Deployment.static_parameters
        with sanitize_dynamic_params(self.dynamic_params) — caller's job
        (see _build_deployment_context). Sanitization MUST happen before
        compose (§10.7 layer-c).

        Mirrors chat-deepagents/session.py::ChatSession.compose_initial_messages.
        """
        ctx_lines = "\n".join(f"  {k}: {v}" for k, v in merged_context.items())
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

    @staticmethod
    def parse_room_metadata(room: Any) -> dict:
        """Parse the LiveKit room.metadata JSON. Required fields validated.

        Per design §10.3.2: the voice frontdoor sets `room.metadata =
        JSON({deployment_id, dynamic_params, interaction_id, correlation_id})`
        at /sessions time. NO auth tokens (room metadata is visible to all
        participants per LiveKit protocol — Gap A from §17.1).

        Args:
            room: A livekit.rtc.Room (or test stub) with a `.metadata` attribute.

        Returns:
            dict with keys `deployment_id` (required str), `interaction_id`
            (optional str), `dynamic_params` (dict, defaults to {}),
            `correlation_id` (optional str).

        Raises:
            ValueError: metadata is empty, not valid JSON, or missing
                deployment_id.
        """
        if not room.metadata:
            raise ValueError(
                "LiveKit room.metadata is empty; expected JSON dict "
                "with at least deployment_id. The voice frontdoor must set this."
            )
        try:
            meta = json.loads(room.metadata)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"LiveKit room.metadata is not valid JSON: {e}"
            ) from e

        if "deployment_id" not in meta:
            raise ValueError(
                "LiveKit room.metadata missing 'deployment_id'. "
                "The voice frontdoor service must set this."
            )

        return {
            "deployment_id": meta["deployment_id"],
            "interaction_id": meta.get("interaction_id"),
            "dynamic_params": meta.get("dynamic_params", {}),
            "correlation_id": meta.get("correlation_id"),
        }

    def __init__(
        self,
        deployment_id: str,
        interaction_id: str | None = None,
        dynamic_params: dict | None = None,
        correlation_id: str | None = None,
        auth_token: str = "",
        checkpointer=None,
    ):
        """Construct a per-room VoiceSession.

        Phase 4 (AI-407 §10.3.2): the voice frontdoor creates the Interaction
        at /sessions time and passes interaction_id + correlation_id +
        dynamic_params via room.metadata. The worker reads them with
        parse_room_metadata + passes them here. associate_id is derived in
        start() from the loaded Deployment (Deployment.associate_id) —
        no more 1:1 Actor.deployment_id assumption.
        """
        self.deployment_id = deployment_id
        self.interaction_id = interaction_id
        self.dynamic_params = dynamic_params or {}
        self.correlation_id = correlation_id
        self.auth_token = auth_token
        self.checkpointer = checkpointer
        # associate_id derived from Deployment in start()
        self.associate_id: str | None = None
        self.attention_id: str | None = None
        self.agent = None
        self.deepagents_llm: DeepagentsLLM | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._events_task: asyncio.Task | None = None
        self._events_process: subprocess.Popen | None = None
        self._event_queue: list[dict] = []
        # Phase 4: composed at start(), stored for the first agent.ainvoke()
        # to prepend; consumed and cleared by the LLM adapter on first user turn.
        self._initial_systemmessages: list | None = None
        # Phase 4: greeting from Deployment (TTS speaks it at session start;
        # persist_greeting_to_state then writes it to checkpointer state per §17.2.22).
        self.greeting: str = ""
        # AI-407 §5.6: effective_actor_id resolved in start() from Deployment.acts_as.
        # Used by _session_indemn wrapper for per-call kwargs on every indemn() call.
        # `assoc:unknown` is a placeholder pre-start — start() always resolves it
        # before the first session-bearing indemn call.
        self._effective_actor_id: str = "assoc:unknown"

    async def start(self) -> None:
        """Initialize the session — load config, open Attention, build agent
        + DeepagentsLLM. Returns once the LLM is ready to plug into a
        LiveKit AgentSession.

        Phase 4 lifecycle (AI-407 §10.3.2):
          1. Load Deployment by self.deployment_id (set in __init__)
          2. Derive self.associate_id = deployment.associate_id (drops the
             1:1 Actor.deployment_id assumption)
          3. Load Associate + Runtime for three-layer LLM config merge
          4. Interaction is NOT created here — the frontdoor already created
             it at /sessions and passed self.interaction_id via room.metadata
          5. Open Attention for real-time session tracking
          6. Build agent + wrap in DeepagentsLLM
        """
        # Load Deployment first (Phase 4: the deployment IS the venue spec).
        # Use bare indemn() for this single bootstrap call — correlation_id +
        # effective_actor_id aren't fully set yet (effective_actor_id depends
        # on acts_as from this same Deployment).
        deployment = indemn("deployment", "get", self.deployment_id)
        log.info(
            "Loaded deployment: %s (%s)",
            deployment.get("name"),
            self.deployment_id,
        )

        # Derive associate_id from Deployment (drops Phase 3's Actor.deployment_id pattern)
        self.associate_id = str(deployment["associate_id"])

        # AI-407 §5.6: resolve effective_actor_id per Deployment.acts_as.
        # All subsequent CLI calls go through self._session_indemn which
        # passes correlation_id + effective_actor_id as per-call kwargs
        # (vs os.environ mutation — uniform with chat per Task 2.11).
        self._effective_actor_id = VoiceSession._resolve_effective_actor_id(
            deployment=deployment,
            dynamic_params=self.dynamic_params,
            associate_id=self.associate_id,
        )
        log.info(
            "Resolved effective_actor_id=%s (acts_as=%s)",
            self._effective_actor_id,
            deployment.get("acts_as", "associate_self"),
        )

        # Load associate config
        associate = self._session_indemn("actor", "get", self.associate_id)
        log.info(
            "Loaded associate: %s (%s)",
            associate.get("name"),
            self.associate_id,
        )

        # Load Runtime config for three-layer merge
        runtime = self._session_indemn("runtime", "get", RUNTIME_ID)

        # Three-layer LLM config merge (Runtime defaults < Associate < Deployment)
        llm_config = _merge_llm_config(runtime, associate, deployment)

        # Interaction was created by the voice frontdoor at /sessions —
        # interaction_id arrived via room.metadata. No need to create here.
        if not self.interaction_id:
            log.warning(
                "VoiceSession.start: no interaction_id from frontdoor — "
                "this indicates a frontdoor bug or local-dev path"
            )

        # Open Attention with purpose=real_time_session — gates scoped watches
        # so mid-conversation entity changes route via the events stream
        attention = await open_attention(
            actor_id=self.associate_id,
            entity_type="Interaction",
            entity_id=self.interaction_id,
            purpose="real_time_session",
            runtime_id=RUNTIME_ID,
        )
        self.attention_id = attention.get("_id")

        # Build the deepagents agent. Phase 4: operating skill arrives as a
        # <skill> SystemMessage at session start (composed below); entity
        # skills still load via CLI on demand at Step 3 of DEFAULT_PROMPT.
        self.agent = build_agent(
            associate=associate,
            llm_config=llm_config,
            checkpointer=self.checkpointer,
        )

        # AI-407 §15.5 voice: compose initial <skill> + <deployment_context>
        # SystemMessages prepended on the first agent.ainvoke() call after
        # the user's first turn. The DeepagentsLLM adapter (Task 2.21) reads
        # _initial_systemmessages, prepends them to the LangChain message
        # list, clears the field. The MongoDB checkpointer (Task 2.16) keyed
        # by interaction_id persists them in state via add_messages reducer.
        skill_xml = self._load_skill_section_xml(associate)
        deployment_context = self._build_deployment_context(associate, deployment)
        self._initial_systemmessages = VoiceSession.compose_initial_messages(
            skill_xml, deployment_context
        )

        # Greeting comes from Deployment (TTS speaks it at session start in
        # main.py; persist_greeting_to_state writes it to checkpointer state
        # AFTER playback so resume doesn't re-greet — §17.2.22).
        self.greeting = deployment.get("greeting", "") or ""

        # Wrap the agent for LiveKit's AgentSession via the LLM adapter.
        # - thread_id binds LangGraph checkpointing to this Interaction so
        #   reconnects pick up the same conversation state.
        # - event_queue is shared with the events-stream subprocess so the
        #   adapter can drain mid-conversation entity changes and inject
        #   them as a SystemMessage on the next user turn.
        # - associate + runtime_id flow into LangSmith metadata so voice
        #   traces are queryable by associate_id / entity_id / runtime_id
        #   (CLAUDE.md § 8 debugging recipe).
        self.deepagents_llm = DeepagentsLLM(
            self.agent,
            thread_id=self.interaction_id,
            event_queue=self._event_queue,
            associate=associate,
            runtime_id=RUNTIME_ID,
            # AI-407 §13.5 voice: correlation_id (LangSmith metadata.thread_id;
            # cascade lineage) + deployment_id (metadata field for cross-pivot
            # search). Both flow into the RunnableConfig built in
            # VoiceSession.build_runnable_config per turn.
            correlation_id=self.correlation_id,
            deployment_id=self.deployment_id,
            # AI-407 Phase 4: initial <skill> + <deployment_context>
            # SystemMessages composed at start() (Task 2.17); consumed on
            # first agent.ainvoke().
            initial_systemmessages=self._initial_systemmessages,
        )

        # Heartbeat keeps Attention alive (TTL = 2 min, refresh every 30s)
        self._heartbeat_task = asyncio.create_task(
            attention_heartbeat_loop(self.attention_id, interval_s=30.0)
        )

        # Events stream gives the agent mid-conversation awareness of entity
        # changes happening outside this voice session (e.g., supervisor
        # updates the Interaction). Same subprocess pattern as chat.
        self._events_task = asyncio.create_task(self._run_events_stream())

        log.info(
            "VoiceSession started: interaction=%s attention=%s",
            self.interaction_id,
            self.attention_id,
        )

    def _load_skill_section_xml(self, associate: dict) -> str:
        """Load operating skill(s) content via CLI, format as nested
        <skill name="X">...</skill> blocks. compose_initial_messages wraps
        the result in an outer <skill>...</skill> for the SystemMessage.

        Mirrors chat-deepagents/session.py::_load_skill_section_xml +
        async-deepagents/main.py::_build_skill_section_xml. Pulled inline
        rather than DRY'd into harness_common to keep Phase 2B's harness-
        common touch surface minimal.
        """
        parts: list[str] = []
        for ref in associate.get("skills") or []:
            try:
                skill = self._session_indemn("skill", "get", ref)
                content = (
                    skill.get("content", "")
                    if isinstance(skill, dict)
                    else str(skill)
                )
                parts.append(f'<skill name="{ref}">')
                parts.append(content)
                parts.append("</skill>")
                parts.append("")
            except CLIError as e:
                log.warning("Failed to load skill %s: %s", ref, e)
        return "\n".join(parts).rstrip()

    def _build_deployment_context(
        self, associate: dict, deployment: dict
    ) -> dict:
        """Build the deployment_context dict for the <deployment_context>
        SystemMessage. Per §10.7 layer-c: sanitize_dynamic_params runs BEFORE
        merging dynamic into static, so user-controlled values can't inject
        pseudo-SystemMessage content into the agent.

        Merge order: static_parameters (operator-trusted, no sanitize needed)
        UNDER sanitized dynamic_params (user-supplied at session start).
        Plus a few session-level fields the agent always wants (deployment_id,
        deployment_name, channel_kind, actor_name).
        """
        safe_dynamic = sanitize_dynamic_params(self.dynamic_params)
        static = deployment.get("static_parameters") or {}
        ctx = {
            "deployment_id": str(deployment.get("_id", self.deployment_id)),
            "deployment_name": deployment.get("name", ""),
            "actor_name": associate.get("name", "Voice Assistant"),
            "channel_kind": "voice",
            **static,
            **safe_dynamic,
        }
        return ctx

    async def persist_greeting_to_state(self, greeting: str) -> None:
        """Append the greeting as an AIMessage to the agent's checkpointer
        state (AI-407 §17.2.22 resolution).

        Called by main.py AFTER the TTS playback completes. Ensures resumed
        sessions (Task 2.35) see the greeting in conversation history and
        don't re-greet. Also makes the LangSmith trace include the greeting
        as part of the conversation.

        Noop if:
        - No checkpointer (degraded mode — no resume capability anyway)
        - No interaction_id (local-dev fallback — can't address state without
          a thread_id)
        - Empty greeting (Deployment.greeting unset)

        Tags the state update with `as_node="greeting"` for trace clarity.
        """
        if not self.checkpointer or not self.interaction_id or not greeting:
            return
        config = {"configurable": {"thread_id": self.interaction_id}}
        try:
            await self.agent.aupdate_state(
                config,
                values={"messages": [AIMessage(content=greeting)]},
                as_node="greeting",
            )
        except Exception as e:
            log.warning(
                "Failed to persist greeting to checkpointer state: %s "
                "(non-fatal — TTS already played the greeting)",
                e,
            )

    async def close(self) -> None:
        """Clean up — cancel background tasks, close Attention + Interaction.

        Idempotent: safe to call from main.py's shutdown handler regardless
        of which step of start() we got to.
        """
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

        log.info("VoiceSession closed: interaction=%s", self.interaction_id)

    async def _run_events_stream(self) -> None:
        """Subscribe to mid-conversation entity events via `indemn events stream`.

        Identical to ChatSession._run_events_stream — the agent gets a feed
        of entity changes related to its working context. Events are queued
        for the agent to drain on its next turn (the AgentSession layer
        decides when to inject them; for voice we keep events in
        self._event_queue and drain when llm_adapter sees a new turn).
        """
        if not self.interaction_id:
            return

        try:
            env = {
                "INDEMN_API_URL": os.environ["INDEMN_API_URL"],
                "INDEMN_SERVICE_TOKEN": os.environ.get(
                    "INDEMN_SERVICE_TOKEN", self.auth_token
                ),
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
                line = await loop.run_in_executor(
                    None, self._events_process.stdout.readline
                )
                if not line:
                    break
                try:
                    event = json.loads(line.decode())
                    self._event_queue.append(event)
                except json.JSONDecodeError:
                    pass

        except asyncio.CancelledError:
            if self._events_process:
                self._events_process.terminate()
        except Exception as e:
            log.warning("Events stream error: %s", e)
