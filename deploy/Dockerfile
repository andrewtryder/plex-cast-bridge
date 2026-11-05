# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    DATA_DIR=/data \
    PATH="/app/.venv/bin:$PATH"

# Install system utilities for mDNS, network discovery, and container health checks
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        avahi-utils \
        libnss-mdns \
        ca-certificates \
        curl && \
    rm -rf /var/lib/apt/lists/*

# Create dedicated non-root system user and group (UID/GID 10001)
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /sbin/nologin -d /app appuser

# Install uv for deterministic dependency resolution
COPY --from=ghcr.io/astral-sh/uv:0.6.5 /uv /bin/uv

WORKDIR /app

# Prepare mutable data directory and application root with non-root ownership
RUN mkdir -p /data && chown -R appuser:appgroup /data /app

# Copy dependency specifications first for optimal layer caching
COPY --chown=appuser:appgroup pyproject.toml uv.lock ./

# Install Python virtualenv dependencies
RUN uv sync --frozen --no-dev --no-install-project

# Copy application source code, assets, and seed catalog
COPY --chown=appuser:appgroup src ./src
COPY --chown=appuser:appgroup assets ./assets
COPY --chown=appuser:appgroup catalog.json ./catalog.json

# Finalize installation into virtualenv
RUN uv sync --frozen --no-dev

# Switch to non-root execution
USER appuser

EXPOSE 8000

# Container healthcheck ensuring local FastAPI service is responding
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8000}/health || exit 1

CMD ["uvicorn", "plex_cast_bridge.main:app", "--host", "0.0.0.0", "--port", "8000"]
