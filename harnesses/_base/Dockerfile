# indemn/harness-base:1.0.0 — shared foundation for all harnesses
FROM python:3.12-slim AS base

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install indemn-os CLI from internal PyPI (pinned version)
# This installs the `indemn` binary on PATH + makes indemn_os importable
ARG INDEMN_OS_VERSION=0.1.0
COPY indemn_os/ /tmp/indemn_os/
RUN cd /tmp/indemn_os && uv pip install --system .

# Install base harness deps
COPY harnesses/_base/pyproject.toml /tmp/_base/
RUN cd /tmp/_base && uv pip install --system .
COPY harnesses/_base/harness_common/ /app/harness_common/

# Agent working directory
RUN mkdir -p /workspace

# Runtime env vars — populated at Railway deploy time:
# INDEMN_API_URL            — where kernel API lives
# INDEMN_SERVICE_TOKEN      — Runtime instance service token
# RUNTIME_ID                — this Runtime's ObjectId
# INDEMN_SANDBOX_TYPE       — localshell (MVP) | daytona (prod)
# OTEL_EXPORTER_OTLP_ENDPOINT — Grafana Cloud OTLP ingress

# Base image is not runnable alone — entry point set by child images
