"""voice-deepagents harness entry point — LiveKit Agents worker.

Connects to Indemn's self-hosted LiveKit (URL/keys via env), spawns one
IndemnVoiceAssistant per room, runs the realtime voice pipeline:

  user audio -> VAD -> STT (Deepgram) -> Gemini LLM -> TTS (Cartesia) -> user audio
                                                |
                                                +-> execute('indemn ...') tool
                                                    -> subprocess to OS CLI

Same Gemini-3-flash-preview model + global location as the async-deepagents
runtime (Bug #42's resolved fix). Tool surface is the `indemn` CLI, symmetric
with how every other Indemn OS associate operates.

Run modes:
- Production: `python main.py start` — connects to the LiveKit URL via env.
- Dev: `python main.py dev` — reads from .env, prints debug logs.

The harness registers itself with the OS Runtime entity (`voice-deepagents-dev`)
on boot so the runtime is discoverable + the actor's Deployment can route
to it. Actors with mode=hybrid + voice-deepagents runtime_id will dispatch
through this worker on each call.
"""

import logging
import os

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
    google,
    silero,
)
from livekit.plugins.turn_detector.english import EnglishModel

from assistant import IndemnVoiceAssistant

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


async def entrypoint(ctx: JobContext) -> None:
    """LiveKit per-room job entrypoint.

    Called once per room a user joins. Constructs the AgentSession with the
    STT/LLM/TTS pipeline + IndemnVoiceAssistant, joins the room, greets the
    user, then runs the voice loop until the room closes.
    """
    log.info("Job started: room=%s", ctx.room.name)

    # Connect to the room (subscribe to all participant audio tracks).
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    # Build the LLM — Gemini-3-flash-preview on global endpoint, matching the
    # async-deepagents runtime default (Bug #42 resolution).
    llm_instance = google.LLM(
        model=os.environ.get("VOICE_LLM_MODEL", "gemini-3-flash-preview"),
        location=os.environ.get("VOICE_LLM_LOCATION", "global"),
        temperature=float(os.environ.get("VOICE_LLM_TEMPERATURE", "0.3")),
    )

    # STT — Deepgram (Indemn standard for voice STT, matches voice-livekit).
    stt_instance = deepgram.STT(
        model=os.environ.get("VOICE_STT_MODEL", "nova-3"),
        language=os.environ.get("VOICE_STT_LANGUAGE", "en"),
    )

    # TTS — Cartesia (Indemn standard).
    tts_instance = cartesia.TTS(
        model=os.environ.get("VOICE_TTS_MODEL", "sonic-3"),
        voice=os.environ.get("VOICE_TTS_VOICE_ID", "6ccbfb76-1fc6-48f7-b71d-91ac6298247b"),
        language=os.environ.get("VOICE_TTS_LANGUAGE", "en"),
    )

    # VAD + turn detector — Silero VAD + LiveKit's English turn detector.
    vad = silero.VAD.load()
    turn_detector = EnglishModel()

    session = AgentSession(
        stt=stt_instance,
        llm=llm_instance,
        tts=tts_instance,
        vad=vad,
        turn_detection=turn_detector,
        # Tighten endpointing for snappier turn-taking on low-latency CLI calls.
        min_endpointing_delay=0.4,
        max_endpointing_delay=2.0,
    )

    assistant = IndemnVoiceAssistant()

    await session.start(room=ctx.room, agent=assistant)

    # Greet the user — the agent's first action will be to load its skill,
    # but we open with a brief "I'm here" so the user knows the line is live.
    await session.say(
        "Hi, this is your Indemn OS assistant. What can I help you with?",
        allow_interruptions=False,
    )


if __name__ == "__main__":
    _setup_gcp_credentials()

    # WorkerOptions tells the LiveKit Agents framework which entrypoint to call
    # per room job. The framework handles room subscription, lifecycle, retries.
    agents.cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
