# indemn/runtime-chat-deepagents — WebSocket server + deepagents
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

# Install chat harness deps (deepagents, starlette, websockets, LangChain)
COPY harnesses/chat-deepagents/pyproject.toml /tmp/chat/
RUN cd /tmp/chat && uv pip install --system .

# Copy harness code
COPY harnesses/chat-deepagents/ /app/harness/

# Agent working directory
RUN mkdir -p /workspace

# Entry point — opentelemetry-instrument for tracing
CMD ["opentelemetry-instrument", "python", "-m", "harness.main"]
