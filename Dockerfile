###########
# builder #
###########
FROM python:3.14-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Versions come from git tags (hatch-vcs), but .git is excluded from the build
# context — the release workflow passes the version in; local builds get a
# dev placeholder.
ARG BRIDGE_VERSION=0.0.0.dev0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${BRIDGE_VERSION}

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src/ src/
COPY adapters/pass-through/ adapters/pass-through/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --extra pass-through

##########
# runner #
##########
FROM python:3.14-slim-bookworm

LABEL org.opencontainers.image.source="https://github.com/Principled-Intelligence/spectral-bridge" \
    org.opencontainers.image.description="spectral-bridge relay client bundled with the pass-through adapter" \
    org.opencontainers.image.licenses="Apache-2.0"

# tini reaps the adapter subprocess and forwards SIGTERM, which Python as
# PID 1 would otherwise ignore (docker stop would hang until SIGKILL).
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 bridge \
    && useradd --uid 10001 --gid 10001 --shell /usr/sbin/nologin --no-create-home bridge

COPY --from=builder /app/.venv /app/.venv
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

WORKDIR /app
USER 10001:10001

# Covers the adapter subprocess; relay connectivity is visible in the logs.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c 'import os,urllib.request; urllib.request.urlopen("http://127.0.0.1:%s/health" % os.environ.get("ADAPTER_PORT", "8840"), timeout=4)'

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
