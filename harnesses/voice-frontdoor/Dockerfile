# Voice frontdoor — Starlette HTTP server for POST /sessions.
# Part of AI-407 Phase 2B (Option C two-services model per design §10.3 + §15.2).
#
# Build context expectation: repo root (so harnesses/_base/ + harnesses/voice-frontdoor/
# both resolve). Build with:
#   docker build -f harnesses/voice-frontdoor/Dockerfile -t indemn-runtime-voice-frontdoor .

FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY harnesses/voice-frontdoor/pyproject.toml /app/pyproject.toml
COPY harnesses/voice-frontdoor/harness /app/harness

# Install deps via uv (no editable; production image)
RUN uv pip install --system --no-cache .

EXPOSE 8080

CMD ["python", "-m", "harness.main"]
