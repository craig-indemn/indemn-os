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
    WorkerOptions,
)
from livekit.plugins import (
    cartesia,
    deepgram,
    silero,
)

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
