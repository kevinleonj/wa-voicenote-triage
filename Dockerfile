# syntax=docker/dockerfile:1.7
#
# wa-voicenote-triage — multi-stage build.
#
# Stage 1 (builder): create a self-contained .venv with production deps only.
# Stage 2 (runtime): minimal python:3.12-slim with ffmpeg + curl + the venv,
# running as a non-root user.
#
# At runtime the entrypoint is `uvicorn wa_voicenote.main:app` — this module
# does not exist until c13. The Dockerfile is forward-compatible: `docker build`
# does not resolve the CMD module, so this builds cleanly today.

# ---- builder ---------------------------------------------------------------
# Astral's official Python + uv image (Debian Trixie slim, Python 3.12, uv
# pre-installed). Pulling from ghcr.io rather than docker.io reduces blast
# radius from Docker Hub outages and avoids a separate uv-binary copy step.
FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_NO_DEV=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (cached layer) — copy only the lockfile and the
# project descriptor, not the source. README is referenced by pyproject.toml's
# `readme = "README.md"` so it must be present for the metadata to resolve.
COPY pyproject.toml uv.lock README.md ./

RUN --mount=type=cache,target=/root/.cache/uv \
    set -eux; \
    uv sync --frozen --no-dev --no-install-project

# Now copy the project and install it into the venv.
COPY src/ ./src/

RUN --mount=type=cache,target=/root/.cache/uv \
    set -eux; \
    uv sync --frozen --no-dev --no-editable

# ---- runtime ---------------------------------------------------------------
# Same base for the runtime stage to avoid pulling a second large image and to
# keep the layer cache aligned. The runtime does not invoke uv — the venv is
# self-contained — but the image is python:3.12 on Debian Trixie slim.
FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim AS runtime

# Install ffmpeg (audio transcode) and curl (HEALTHCHECK). `--no-install-recommends`
# keeps the image lean; the cleanup in the same RUN avoids leaving the apt cache
# in a layer. Pinning every apt package version on a slim base is brittle and
# blocks security updates — DL3008 is ignored in .hadolint.yaml with a reason.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates; \
    rm -rf /var/lib/apt/lists/*

# Non-root user. Fixed uid 10001 matches Container Apps' recommended high-uid
# convention to avoid clashing with host users.
RUN set -eux; \
    groupadd --system --gid 10001 appuser; \
    useradd --system --uid 10001 --gid appuser --home-dir /app --shell /usr/sbin/nologin appuser

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Copy the venv from the builder. Ownership flipped to appuser so the runtime
# user can read every file without needing root.
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv

# Copy source. main:app will land in c13 — building today still succeeds because
# Docker does not introspect the CMD.
COPY --chown=appuser:appuser src/ /app/src/

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "wa_voicenote.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers"]
