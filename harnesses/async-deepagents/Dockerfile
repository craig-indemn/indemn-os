# indemn/runtime-async-deepagents — self-contained for Railway deploy
FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install indemn-os CLI package
COPY indemn_os/ /tmp/indemn_os/
RUN cd /tmp/indemn_os && uv pip install --system .

# Install base harness deps
COPY harnesses/_base/pyproject.toml /tmp/_base/
RUN cd /tmp/_base && uv pip install --system .
COPY harnesses/_base/harness_common/ /app/harness_common/

# Install async harness deps (deepagents, temporal SDK, LangChain)
COPY harnesses/async-deepagents/pyproject.toml /tmp/async/
RUN cd /tmp/async && uv pip install --system .

# Copy harness code (bust cache: 2026-05-12-b)
COPY harnesses/async-deepagents/ /app/harness/

# Smoke test — verify imports work before deploying. Three consecutive
# deploys in Session 19 introduced import errors that crashed the runtime.
RUN python -c "from harness.agent import build_agent; print('harness smoke test passed')"

# Agent working directory
RUN mkdir -p /workspace

# Entry point — opentelemetry-instrument auto-reads OTEL_* env vars
CMD ["opentelemetry-instrument", "python", "-m", "harness.main"]
