# syntax=docker/dockerfile:1
# Ubuntu is required by playwright
FROM ubuntu:latest AS base

ARG GITHUB_BUILD=false \
    VERSION

ENV GITHUB_BUILD=${GITHUB_BUILD}\
    VERSION=${VERSION}\
    DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    # prevents python creating .pyc files
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    PORT=8191 \
    XDG_CACHE_HOME=/cache \
    HOME=/tmp

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    curl ca-certificates libgtk-3-0 libdbus-glib-1-2 libxt6 libasound2t64 libnss3 libx11-xcb1 libgbm1
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

FROM base AS devcontainer
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,target=/cache/uv,sharing=locked \
    --mount=type=cache,target=/tmp/camoufox-cache,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends git && \
    mkdir -p /cache/camoufox && \
    XDG_CACHE_HOME=/tmp/camoufox-cache uvx camoufox fetch && \
    cp -a /tmp/camoufox-cache/camoufox/. /cache/camoufox/ && \
    uvx playwright install-deps firefox
ENTRYPOINT [ "sleep", "infinity" ]

FROM base AS app
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,target=/cache/uv,sharing=locked \
    --mount=type=cache,target=/tmp/camoufox-cache,sharing=locked \
    mkdir -p /cache/camoufox && \
    uv sync && \
    CAMOUFOX_DIR=$(uv run python -c "import camoufox; import os; print(os.path.dirname(camoufox.__file__))") && \
    if [ -f /tmp/camoufox-cache/GeoLite2-City.mmdb ]; then \
        cp /tmp/camoufox-cache/GeoLite2-City.mmdb "$CAMOUFOX_DIR/"; \
    fi && \
    XDG_CACHE_HOME=/tmp/camoufox-cache uv run camoufox fetch && \
    cp "$CAMOUFOX_DIR/GeoLite2-City.mmdb" /tmp/camoufox-cache/ && \
    cp -a /tmp/camoufox-cache/camoufox/. /cache/camoufox/ && \
    apt-get update && \
    uv run playwright install-deps firefox

COPY . .

# Make app and cache world-readable; addon scripts dir world-writable (runtime writes)
RUN chmod -R o+rX /app /cache &&\
    find /app/.venv -path "*/camoufox_add_init_script/addon" -type d -exec chmod -R o+rwX {} +

FROM app AS test
RUN --mount=type=cache,target=/cache/uv,sharing=locked \
    uv sync --group test && \
    uv run pytest --retries 3

FROM app
USER 1000
EXPOSE $PORT
HEALTHCHECK --interval=15m --timeout=30s --start-period=5s --retries=3 CMD curl "http://localhost:${PORT}/health"
ENTRYPOINT ["/app/.venv/bin/python", "main.py"]
