# indemn/runtime-voice-deepagents — LiveKit Agents worker
# Realtime voice via LiveKit + Gemini + indemn CLI tool surface.
FROM python:3.12-slim

WORKDIR /app

# System libraries needed by LiveKit's audio stack (PortAudio, libsndfile)
# and by Whisper-class VAD/STT models (FFmpeg).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    libportaudio2 \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install indemn-os CLI package — gives the harness the `indemn` command on PATH
COPY indemn_os/ /tmp/indemn_os/
RUN cd /tmp/indemn_os && uv pip install --system .

# Install base harness deps (effective_actor_id propagation, harness_common)
COPY harnesses/_base/pyproject.toml /tmp/_base/
RUN cd /tmp/_base && uv pip install --system .
COPY harnesses/_base/harness_common/ /app/harness_common/

# Install voice harness deps (livekit-agents + plugins)
COPY harnesses/voice-deepagents/pyproject.toml /tmp/voice/
RUN cd /tmp/voice && uv pip install --system .

# Pre-download VAD + turn-detector models so they're cached at image build time
# (avoids per-cold-start download spike on the first call).
RUN python -c "from livekit.plugins import silero; silero.VAD.load()" || true
RUN python -c "from livekit.plugins.turn_detector.english import EnglishModel; EnglishModel()" || true

# Copy harness code
COPY harnesses/voice-deepagents/main.py /app/main.py
COPY harnesses/voice-deepagents/assistant.py /app/assistant.py
COPY harnesses/voice-deepagents/tools.py /app/tools.py

# Entry point — `start` connects to LiveKit URL; `dev` for local debugging.
CMD ["python", "main.py", "start"]
