FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application code
COPY kernel/ kernel/
COPY kernel_entities/ kernel_entities/
COPY seed/ seed/

# Default entry point (overridden per Railway service)
CMD ["uv", "run", "python", "-m", "kernel.api.app"]
