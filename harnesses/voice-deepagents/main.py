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
from livekit import agents
from livekit.agents import (
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobProcess,
    WorkerOptions,
)
from livekit.plugins import (
    cartesia,
    deepgram,
    silero,
)
from livekit.plugins.turn_detector.english import EnglishModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("voice-deepagents")


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


# Sole-tenant: one associate per voice runtime. The associate id comes from
# env so the harness image stays generic and the OS routes voice calls to
# whichever actor owns this Runtime instance. Async + chat use the same
# convention (the async harness reads associate from message; the chat
# harness reads from the WebSocket connect message; voice reads from env).
def _voice_associate_id() -> str:
    aid = os.environ.get("VOICE_ASSOCIATE_ID", "")
    if not aid:
        # Fallback for local dev — harness still boots but greets that
        # the associate isn't configured. Real deployments set this.
        log.warning("VOICE_ASSOCIATE_ID not set — voice will be unconfigured")
    return aid


async def entrypoint(ctx: JobContext) -> None:
    """LiveKit per-room job entrypoint.

    Called once per room a user joins. Constructs a VoiceSession (which
    creates Interaction + Attention + builds the deepagents agent), then
    plugs the deepagents-backed LLM into a LiveKit AgentSession with
    Deepgram STT + Cartesia TTS + Silero VAD. Joins the room, greets the
    user, runs the voice loop until the room closes.
    """
    log.info("Job started: room=%s", ctx.room.name)

    associate_id = _voice_associate_id()
    auth_token = os.environ.get("INDEMN_SERVICE_TOKEN", "")

    # OS-side session lifecycle: load config, create Interaction + Attention,
    # build the deepagents agent, wrap it in DeepagentsLLM.
    voice_session = VoiceSession(
        associate_id=associate_id,
        auth_token=auth_token,
        # Checkpointer is per-runtime concern; voice uses the in-memory
        # default for now. MongoDB checkpointer can be wired later
        # alongside chat-deepagents (commit `7281b83` follow-up).
        checkpointer=None,
    )
    if associate_id:
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

    # VAD + turn detector
    vad = silero.VAD.load()
    turn_detector = EnglishModel()

    # AgentSession with our DeepagentsLLM as the LLM. The deepagents agent
    # handles ALL reasoning + tool calls internally — LiveKit just sees a
    # standard LLM that takes ChatContext and emits ChatChunks.
    agent_session = AgentSession(
        stt=stt_instance,
        llm=voice_session.deepagents_llm,
        tts=tts_instance,
        vad=vad,
        turn_detection=turn_detector,
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


def prewarm(proc: JobProcess) -> None:
    """Per-JobProcess initialization — registers this worker as an OS Runtime
    instance and spawns a daemon thread to run the Attention/Runtime heartbeat
    loop in its own asyncio event loop.

    Why a daemon thread (not asyncio.create_task in the main loop):
    LiveKit Agents runs each JobProcess in its own subprocess; the worker's
    asyncio event loop is fully managed by `agents.cli.run_app()` and we
    don't have a hook to schedule long-running tasks on it. A daemon thread
    with its own event loop is the standard way to run a background heartbeat
    that lives for the JobProcess's lifetime — when the JobProcess dies,
    the daemon thread dies with it, and the OS-side queue processor's
    `cleanup_expired_attentions` sweep + `last_heartbeat` TTL handle the
    Runtime instance state machine via the kernel side.

    This mirrors the OS architecture intent (one Runtime instance per
    worker process; each registers + heartbeats independently) — see
    `docs/architecture/realtime.md` Runtime section + `harness_common/
    runtime.py`. The async-deepagents + chat-deepagents harnesses run
    register_instance + heartbeat_loop inline because their event loops
    are owned by Temporal worker / Starlette lifespan respectively;
    LiveKit Agents doesn't expose an equivalent hook so we use a thread.
    """
    if not RUNTIME_ID:
        log.warning("RUNTIME_ID env var not set — skipping runtime registration")
        return

    try:
        indemn("runtime", "register-instance", "--runtime-id", RUNTIME_ID)
        log.info("Registered Runtime instance for runtime_id=%s", RUNTIME_ID)
    except Exception as e:
        # Don't fail the worker — runtime registration is best-effort.
        # Heartbeat thread starts anyway; missing register-instance just
        # means the OS Runtime entity won't have this worker in its
        # `instances` list initially. The heartbeat will create it.
        log.warning("Runtime register-instance failed: %s", e)

    threading.Thread(
        target=lambda: asyncio.run(heartbeat_loop(interval_s=30.0)),
        name="indemn-runtime-heartbeat",
        daemon=True,
    ).start()
    log.info("Heartbeat daemon thread started (interval=30s)")


if __name__ == "__main__":
    _setup_gcp_credentials()

    # WorkerOptions tells LiveKit which entrypoint to call per room job.
    # `prewarm_fnc` runs once per JobProcess subprocess; we use it for the
    # one-time runtime-instance registration + heartbeat thread launch.
    agents.cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
        )
    )
