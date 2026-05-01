# indemn/runtime-voice-deepagents — LiveKit Agents transport over the deepagents agent
# Mirrors chat-deepagents Dockerfile structure: same harness_common, same indemn CLI,
# same agent code (deepagents + LangChain) — only the I/O transport (LiveKit) differs.
FROM python:3.12-slim

WORKDIR /app

# System libs needed by LiveKit's audio stack (PortAudio, libsndfile) +
# FFmpeg for VAD model fetches.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    libportaudio2 \
    && rm -rf /var/lib/apt/lists/*

# uv for fast pip installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Indemn OS CLI — gives the agent's `execute` tool the `indemn` command on PATH.
COPY indemn_os/ /tmp/indemn_os/
RUN cd /tmp/indemn_os && uv pip install --system .

# Base harness deps (effective_actor_id propagation, harness_common shared lib).
COPY harnesses/_base/pyproject.toml /tmp/_base/
RUN cd /tmp/_base && uv pip install --system .
COPY harnesses/_base/harness_common/ /app/harness_common/

# Voice harness deps — deepagents, LangChain, livekit-agents + plugins.
COPY harnesses/voice-deepagents/pyproject.toml /tmp/voice/
RUN cd /tmp/voice && uv pip install --system .

# Pre-download VAD + turn-detector models so they're cached at image build time
# (avoids per-cold-start download spike on the first call).
RUN python -c "from livekit.plugins import silero; silero.VAD.load()" || true
RUN python -c "from livekit.plugins.turn_detector.english import EnglishModel; EnglishModel()" || true

# Copy harness code as a Python package — `python -m harness.main` resolves to /app/harness/main.py.
# Same layout as chat-deepagents.
COPY harnesses/voice-deepagents/ /app/harness/

# Agent working directory (skills filesystem live here per VoiceSession.start)
RUN mkdir -p /workspace

# Entry point — opentelemetry-instrument for tracing, then the harness module.
CMD ["opentelemetry-instrument", "python", "-m", "harness.main"]
