# Multi-stage build.
#   development → includes dev dependencies, used by compose `--profile full`
#   production  → runtime dependencies only, non-root, no tests
#
# Python version matches local development. See ADR-005.

# --- base ------------------------------------------------------------------
FROM python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl is needed for the container healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# --- dependencies ----------------------------------------------------------
# Copied alone so dependency layers cache independently of source changes.
#
# Installed from /tmp/build rather than /app: pip writes build/ and *.egg-info
# next to pyproject.toml, and those artifacts would otherwise linger in the
# runtime WORKDIR where .dockerignore cannot reach them.
FROM base AS deps
COPY pyproject.toml /tmp/build/
RUN pip install --no-cache-dir /tmp/build

# --- development -----------------------------------------------------------
FROM deps AS development
RUN pip install --no-cache-dir "/tmp/build[dev]" \
    && rm -rf /tmp/build
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# --- production ------------------------------------------------------------
FROM deps AS production

RUN rm -rf /tmp/build

# Run unprivileged: a container escape should not land on root.
RUN useradd --create-home --uid 1000 appuser

# Only what the runtime needs. Tests and tooling config stay out of the image.
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser alembic ./alembic
COPY --chown=appuser:appuser alembic.ini ./

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
