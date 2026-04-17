FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install indemn-os CLI package (kernel depends on it via path)
COPY indemn_os/ indemn_os/

# Install all dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application code
COPY kernel/ kernel/
COPY kernel_entities/ kernel_entities/
COPY seed/ seed/

# Entrypoint dispatches based on SERVICE_TYPE env var
COPY entrypoint.sh /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]
