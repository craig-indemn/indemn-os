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
# AI-406: per-vendor JSON Schema files for SurfaceConfig.config validation
# (kernel/schema_validation.py reads these at SurfaceConfig instance construction —
# including Beanie's load path — so they must be present in the image).
COPY schemas/ schemas/

# Entrypoint dispatches based on SERVICE_TYPE env var
COPY entrypoint.sh /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]
