# Voice frontdoor — Starlette HTTP server for POST /sessions.
# Part of AI-407 Phase 2B (Option C two-services model per design §10.3 + §15.2).
#
# Build context expectation: repo root (so harnesses/_base/ + harnesses/voice-frontdoor/
# both resolve). Build with:
#   docker build -f harnesses/voice-frontdoor/Dockerfile -t indemn-runtime-voice-frontdoor .

FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

# Install base harness deps + harness_common module
# AI-408 extracted JWT validation to harness_common/jwt_auth.py; voice-frontdoor's
# harness/jwt_auth.py wraps it. ba3feea removed this COPY+install when voice-frontdoor
# was self-contained pre-AI-408 — now it needs to come back. Mirrors the chat-deepagents
# Dockerfile pattern: install harness-common package's deps, then drop the actual
# module at /app/harness_common/ so Python imports resolve.
COPY harnesses/_base/pyproject.toml /tmp/_base/
RUN cd /tmp/_base && uv pip install --system --no-cache .
COPY harnesses/_base/harness_common/ /app/harness_common/

COPY harnesses/voice-frontdoor/pyproject.toml /app/pyproject.toml
COPY harnesses/voice-frontdoor/harness /app/harness

# Install deps via uv (no editable; production image)
RUN uv pip install --system --no-cache .

EXPOSE 8080

CMD ["python", "-m", "harness.main"]
