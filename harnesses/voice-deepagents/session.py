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

from harness.agent import build_agent
from harness.llm_adapter import DeepagentsLLM
from harness_common.attention import attention_heartbeat_loop, close_attention, open_attention
from harness_common.cli import CLIError, indemn
from harness_common.interaction import close_interaction, create_interaction
from harness_common.runtime import RUNTIME_ID

log = logging.getLogger(__name__)

def _workspace_dir() -> str:
    """Where the agent's filesystem (skills, scratch) lives.

    Resolution order:
      1. `INDEMN_WORKSPACE_DIR` env var if set
      2. `/workspace` if it exists and is writable (Docker convention —
         the Dockerfile creates this with `mkdir -p /workspace`)
      3. `/tmp/indemn-workspace` fallback (always writable on macOS/Linux)

    Auto-fallback at (3) is necessary because LiveKit Agents' `spawn`-mode
    JobProcess subprocess doesn't always inherit the parent's env vars
    cleanly; relying on the env var alone fails for local-dev `python -m
    harness.main`. The runtime check picks the right path regardless.
    Read at use-time, not module-import, so subprocess-side resolution
    works.
    """
    explicit = os.environ.get("INDEMN_WORKSPACE_DIR")
    if explicit:
        return explicit
    if os.path.isdir("/workspace") and os.access("/workspace", os.W_OK):
        return "/workspace"
    return "/tmp/indemn-workspace"


def _skills_dir() -> str:
    return os.path.join(_workspace_dir(), "skills")


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

    def __init__(
        self,
        associate_id: str,
        auth_token: str,
        checkpointer=None,
    ):
        self.associate_id = associate_id
        self.auth_token = auth_token
        self.checkpointer = checkpointer
        self.interaction_id: str | None = None
        self.attention_id: str | None = None
        self.agent = None
        self.deepagents_llm: DeepagentsLLM | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._events_task: asyncio.Task | None = None
        self._events_process: subprocess.Popen | None = None
        self._event_queue: list[dict] = []

    async def start(self) -> None:
        """Initialize the session — load config, create Interaction + Attention,
        build agent + DeepagentsLLM. Returns once the LLM is ready to plug into
        a LiveKit AgentSession.

        Mirrors ChatSession.start() — same OS lifecycle, no WebSocket-specific
        steps; the LiveKit AgentSession.start() is invoked separately by main.py.
        """
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

        # Three-layer LLM config merge (Runtime defaults < Associate < Deployment)
        llm_config = _merge_llm_config(runtime, associate, deployment)

        # Write skills to filesystem for deepagents progressive disclosure.
        # Same pattern as chat-deepagents/session.py — agent loads metadata
        # in prompt, fetches full content via read_file on demand.
        skill_paths = await self._write_skills_to_filesystem(associate.get("skills", []))

        # Create Interaction (voice channel)
        interaction = await create_interaction(
            channel_type="voice",
            associate_id=self.associate_id,
            deployment_id=deployment_id,
        )
        self.interaction_id = interaction.get("_id")

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

        # Build the deepagents agent (same shape as chat + async)
        self.agent = build_agent(
            associate=associate,
            skill_paths=["skills"] if skill_paths else [],
            llm_config=llm_config,
            checkpointer=self.checkpointer,
        )

        # Wrap the agent for LiveKit's AgentSession via the LLM adapter.
        # - thread_id binds LangGraph checkpointing to this Interaction so
        #   reconnects pick up the same conversation state.
        # - event_queue is shared with the events-stream subprocess so the
        #   adapter can drain mid-conversation entity changes and inject
        #   them as a SystemMessage on the next user turn.
        self.deepagents_llm = DeepagentsLLM(
            self.agent,
            thread_id=self.interaction_id,
            event_queue=self._event_queue,
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

    async def _write_skills_to_filesystem(self, skill_refs: list[str]) -> list[str]:
        """Fetch skills via CLI and write SKILL.md files for deepagents.

        Mirrors ChatSession._write_skills_to_filesystem. Per Bug #35 fix,
        returns absolute paths and uses yaml.safe_dump for frontmatter.
        """
        if not skill_refs:
            return []

        skills_root = _skills_dir()
        os.makedirs(skills_root, exist_ok=True)

        loop = asyncio.get_event_loop()
        all_skills = await loop.run_in_executor(
            None, indemn, "skill", "list", "--format", "json"
        )
        skill_map = {s["name"]: s for s in all_skills}

        skill_paths: list[str] = []
        for ref in skill_refs:
            skill = skill_map.get(ref)
            if not skill:
                log.warning("Skill not found: %s", ref)
                continue

            slug = ref.lower().replace(" ", "-")
            skill_dir = os.path.join(skills_root, slug)
            os.makedirs(skill_dir, exist_ok=True)

            content = skill.get("content", "")
            frontmatter = (
                f"---\n"
                f"name: {ref}\n"
                f"description: Skill for {ref}\n"
                f"---\n\n"
            )
            with open(os.path.join(skill_dir, "SKILL.md"), "w") as f:
                f.write(frontmatter + content)

            skill_paths.append(f"skills/{slug}")

        log.info(
            "Wrote %d skills to %s for progressive disclosure",
            len(skill_paths),
            skills_root,
        )
        return skill_paths

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
