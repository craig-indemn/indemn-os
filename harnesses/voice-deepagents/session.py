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
        # Load Deployment first (Phase 4: the deployment IS the venue spec)
        deployment = indemn("deployment", "get", self.deployment_id)
        log.info(
            "Loaded deployment: %s (%s)",
            deployment.get("name"),
            self.deployment_id,
        )

        # Derive associate_id from Deployment (drops Phase 3's Actor.deployment_id pattern)
        self.associate_id = str(deployment["associate_id"])

        # Load associate config
        associate = indemn("actor", "get", self.associate_id)
        log.info(
            "Loaded associate: %s (%s)",
            associate.get("name"),
            self.associate_id,
        )

        # Load Runtime config for three-layer merge
        runtime = indemn("runtime", "get", RUNTIME_ID)

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

        # Build the deepagents agent — skills load via CLI directives in the
        # system prompt (commit `7281b83` pattern), no filesystem skill writing.
        self.agent = build_agent(
            associate=associate,
            llm_config=llm_config,
            checkpointer=self.checkpointer,
        )

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
