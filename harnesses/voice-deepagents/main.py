"""voice-deepagents harness entry point — LiveKit Agents worker.

Connects to Indemn's self-hosted LiveKit (URL/keys via env), spawns one
VoiceSession per room, runs the canonical realtime voice pipeline:

  user audio -> VAD -> STT (Deepgram)
                            |
                            v
                      DeepagentsLLM
                       (deepagents)
                            |
              (executes indemn CLI internally,
               loads skills, plans w/ todos, etc.)
                            |
                            v
                  TTS (Cartesia) -> user audio

Same Gemini-3-flash-preview model + global location as the async-deepagents
runtime (Bug #42 resolution); inherited from runtime defaults via the
three-layer config merge in session.py.

The harness registers itself with the OS Runtime entity (`voice-deepagents-dev`)
on boot so the runtime is discoverable + actors with that runtime_id dispatch
through this worker on each call.

Run modes:
- Production: `python -m harness.main` (Dockerfile entrypoint)
- Dev: `python -m harness.main dev` for local debugging
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

from harness.session import VoiceSession
from harness_common.cli import indemn
from harness_common.runtime import RUNTIME_ID, heartbeat_loop
from langgraph.checkpoint.mongodb import MongoDBSaver
from livekit import agents
from livekit.agents import (
    AgentSession,
    AutoSubscribe,
    JobContext,
    WorkerOptions,
)
from livekit.plugins import (
    cartesia,
    deepgram,
    silero,
)
from motor.motor_asyncio import AsyncIOMotorClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("voice-deepagents")


# AI-407 Task 2.16: MongoDB checkpointer for voice — real-time sessions
# accumulate state across turns; resume across reconnects (Task 2.35)
# loads prior state via interaction_id thread (§13.3).
#
# Why module-level lazy init + asyncio.Lock (vs chat's Starlette lifespan):
# LiveKit Agents (`agents.cli.run_app(WorkerOptions(...))`) owns its own
# event loop and dispatches per-room jobs into it. There's no Starlette
# app to attach a lifespan to. Module-level cache + asyncio.Lock makes
# init safe under concurrent room dispatches.
#
# The cache uses three states: None (not yet attempted), MongoDBSaver
# instance (initialized successfully), False (tried + failed; don't retry).
_checkpointer = None
_checkpointer_init_lock: asyncio.Lock | None = None


async def _get_or_init_checkpointer():
    """Lazy MongoDBSaver init. Returns None if MONGODB_URI is absent or
    unreachable — voice falls back to per-turn in-memory state (no resume),
    matching today's degraded behavior.

    Mirrors chat-deepagents/main.py:_init_checkpointer_at_startup but without
    the Starlette lifespan dependency (LiveKit Agents owns the event loop
    and dispatches per-room jobs into it; no lifespan hook available).
    """
    global _checkpointer, _checkpointer_init_lock
    # Lock created lazily so module-import doesn't need an event loop
    if _checkpointer_init_lock is None:
        _checkpointer_init_lock = asyncio.Lock()
    async with _checkpointer_init_lock:
        # `False` sentinel = "tried + failed; don't keep retrying"
        if _checkpointer is False:
            return None
        if _checkpointer is not None:
            return _checkpointer
        mongodb_uri = os.environ.get("MONGODB_URI", "")
        if not mongodb_uri:
            log.warning(
                "MONGODB_URI not set — voice checkpointer disabled (no resume)"
            )
            _checkpointer = False
            return None
        try:
            motor_client = AsyncIOMotorClient(mongodb_uri)
            await motor_client.admin.command("ping")  # verify reachable
            _checkpointer = MongoDBSaver(
                motor_client.delegate, db_name="indemn_os_checkpoints"
            )
            log.info("Voice MongoDB checkpointer initialized")
            return _checkpointer
        except Exception as e:
            log.warning(
                "Voice MongoDB checkpointer unavailable: %s — degraded mode",
                e,
            )
            _checkpointer = False
            return None


def _setup_gcp_credentials() -> None:
    """Write GCP service account JSON to file if provided via env var.

    Mirrors the chat-deepagents/async-deepagents pattern: Railway env vars
    store \\n as literal backslash-n; PEM keys need actual newlines.
    """
    sa_json = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        return
    try:
        import json as json_mod

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


async def entrypoint(ctx: JobContext) -> None:
    """LiveKit per-room job entrypoint.

    Called once per room a user joins. Per AI-407 §10.3.2: the voice frontdoor
    creates the Interaction + LiveKit room (with metadata) at /sessions time;
    the worker reads deployment_id + dynamic_params + interaction_id +
    correlation_id from room.metadata, loads the Deployment to derive the
    associate, opens Attention, builds the deepagents agent, then plugs the
    deepagents-backed LLM into a LiveKit AgentSession with Deepgram STT +
    Cartesia TTS + Silero VAD. Joins the room, greets the user, runs the
    voice loop until the room closes.
    """
    log.info("Job started: room=%s", ctx.room.name)

    # AI-407 §10.3.2: parse the frontdoor-supplied context out of room.metadata.
    # NO auth tokens here (visible to all participants per LiveKit protocol).
    # The worker authenticates via its own INDEMN_SERVICE_TOKEN env var.
    try:
        meta = VoiceSession.parse_room_metadata(ctx.room)
    except ValueError as e:
        log.error("Cannot start voice session: %s", e)
        return

    auth_token = os.environ.get("INDEMN_SERVICE_TOKEN", "")

    # OS-side session lifecycle: load Deployment (derive associate from it),
    # load Runtime + LLM config, attach to existing Interaction (already
    # created by frontdoor), open Attention, build agent, wrap in DeepagentsLLM.
    # MongoDB checkpointer — keyed by interaction_id per §13.3. Lazy init at
    # first call (module-level cache shared across rooms). Returns None if
    # MONGODB_URI is unset or Mongo is unreachable (degraded mode — no resume).
    checkpointer = await _get_or_init_checkpointer()

    voice_session = VoiceSession(
        deployment_id=meta["deployment_id"],
        interaction_id=meta["interaction_id"],
        dynamic_params=meta["dynamic_params"],
        correlation_id=meta["correlation_id"],
        auth_token=auth_token,
        checkpointer=checkpointer,
    )
    await voice_session.start()

    # STT — Deepgram, matching voice-livekit (Indemn's customer voice product).
    stt_instance = deepgram.STT(
        model=os.environ.get("VOICE_STT_MODEL", "nova-3"),
        language=os.environ.get("VOICE_STT_LANGUAGE", "en"),
    )

    # TTS — Cartesia, matching voice-livekit.
    tts_instance = cartesia.TTS(
        model=os.environ.get("VOICE_TTS_MODEL", "sonic-3"),
        voice=os.environ.get("VOICE_TTS_VOICE_ID", "6ccbfb76-1fc6-48f7-b71d-91ac6298247b"),
        language=os.environ.get("VOICE_TTS_LANGUAGE", "en"),
    )

    # VAD-only turn detection. EnglishModel (the multilingual ML turn detector)
    # is optional — adds accuracy for tricky boundaries but requires huggingface
    # model files at runtime. For internal team voice (predictable phrasing,
    # short turns), Silero VAD's silence detection is sufficient. Re-enable
    # later if turn-taking quality requires it (would need to wire the model
    # download into the Dockerfile reliably).
    vad = silero.VAD.load()

    # AgentSession with our DeepagentsLLM as the LLM. The deepagents agent
    # handles ALL reasoning + tool calls internally — LiveKit just sees a
    # standard LLM that takes ChatContext and emits ChatChunks.
    agent_session = AgentSession(
        stt=stt_instance,
        llm=voice_session.deepagents_llm,
        tts=tts_instance,
        vad=vad,
        # Tighter endpointing for snappier turn-taking on internal CLI calls.
        min_endpointing_delay=0.4,
        max_endpointing_delay=2.0,
    )

    # Connect to the room (subscribe to all participant audio).
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    # Bare LiveKit `Agent` — the AgentSession needs an Agent instance for
    # its protocol but our LLM is the one doing the work. The Agent here
    # just provides the interface contract; instructions are unused
    # because DeepagentsLLM has the full system prompt baked in via
    # build_agent.
    bare_agent = agents.Agent(instructions="(handled by DeepagentsLLM)")

    await agent_session.start(room=ctx.room, agent=bare_agent)

    # Greet the user. The deepagents agent will load its skill on the
    # first user turn — this opener just confirms the line is live.
    await agent_session.say(
        "Hi, this is your Indemn OS assistant. What can I help you with?",
        allow_interruptions=False,
    )

    # Hold the entrypoint open until the room disconnects. AgentSession
    # runs the voice loop in the background; we just need to wait.
    try:
        await ctx.wait_for_disconnect()
    except AttributeError:
        # Older livekit-agents versions name this differently; fall through
        # to a long sleep until the worker shuts the job.
        while True:
            await asyncio.sleep(60)
    finally:
        await voice_session.close()


def _register_with_os_runtime() -> None:
    """Register this worker with the OS Runtime entity + start heartbeat thread.

    Runs in the main worker process at boot, BEFORE `agents.cli.run_app()` takes
    over the event loop. We do this synchronously (the `indemn` CLI is a
    subprocess — no event loop needed) and spawn a daemon thread for the
    heartbeat loop with its own asyncio event loop.

    Why not in `prewarm_fnc`: prewarm fires per-JobProcess, not on worker boot.
    The OS Runtime entity needs to see "this worker is alive" before any job
    arrives — otherwise dispatchable jobs wouldn't see the runtime as `active`
    and the auto-dispatch path would skip us.

    Why a daemon thread for the heartbeat: `agents.cli.run_app()` owns its
    own asyncio event loop and we don't have a public hook to schedule tasks
    on it. A daemon thread with its own loop runs independently for the
    process's lifetime — when the worker dies, the thread dies with it, and
    the kernel-side `cleanup_expired_attentions` sweep + 2-min TTL handle
    Runtime state machine.

    This is the analog of chat-deepagents/main.py's Starlette `lifespan`
    context manager (which calls `register_instance` + `create_task(
    heartbeat_loop)` once on app boot). LiveKit Agents doesn't expose
    `lifespan`, so we register before `run_app` instead.
    """
    if not RUNTIME_ID:
        log.warning("RUNTIME_ID env var not set — skipping runtime registration")
        return

    try:
        indemn("runtime", "register-instance", "--runtime-id", RUNTIME_ID)
        log.info("Registered Runtime instance for runtime_id=%s", RUNTIME_ID)
    except Exception as e:
        # Don't fail the worker — runtime registration is best-effort.
        # The heartbeat thread starts anyway; if it goes through, the kernel
        # side will create the instance entry on the first heartbeat sweep.
        log.warning("Runtime register-instance failed: %s", e)

    threading.Thread(
        target=lambda: asyncio.run(heartbeat_loop(interval_s=30.0)),
        name="indemn-runtime-heartbeat",
        daemon=True,
    ).start()
    log.info("Heartbeat daemon thread started (interval=30s)")


if __name__ == "__main__":
    _setup_gcp_credentials()

    # Register this worker with the OS Runtime entity FIRST so the entity
    # transitions to `active` before any room job arrives. Heartbeat runs in a
    # daemon thread independent of LiveKit's event loop.
    _register_with_os_runtime()

    # WorkerOptions tells LiveKit which entrypoint to call per room job.
    # `agent_name` enables explicit-dispatch routing — only LiveKit rooms that
    # explicitly request this agent (via room config or AgentDispatch API) are
    # routed to this worker. Without it, the worker accepts auto-dispatch and
    # would pick up ANY room that has no agent specified — including customer
    # voice-livekit traffic. Same pattern as voice-livekit/main.py.
    agent_name = os.environ.get("AGENT_NAME", "voice-deepagents")

    agents.cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=agent_name,
        )
    )
