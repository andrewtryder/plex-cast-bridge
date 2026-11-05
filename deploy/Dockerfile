# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# Install system utilities for mDNS and network discovery
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        avahi-utils \
        libnss-mdns \
        ca-certificates \
        curl && \
    rm -rf /var/lib/apt/lists/*

# Install uv for fast, deterministic dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy dependency specifications
COPY pyproject.toml uv.lock ./

# Install dependencies into virtualenv
RUN uv sync --frozen --no-dev --no-install-project

# Copy application source code and catalog
COPY src ./src
COPY catalog.json ./

# Install project
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["uvicorn", "plex_cast_bridge.main:app", "--host", "0.0.0.0", "--port", "8000"]

