# syntax=docker/dockerfile:1
# Multi-stage backend image: compile dependencies in the build stage, then copy wheels into a minimal runtime.

# ---------------------------------------------------------------------------
# build: resolve and compile every wheel
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN apt-get update \
 && apt-get install --yes --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy pyproject.toml first so source-only changes can reuse the dependency layer.
COPY pyproject.toml ./
RUN pip wheel --wheel-dir /wheels .

# ---------------------------------------------------------------------------
# runtime: the wheels, the app, and nothing else
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libpq5 is required by asyncpg; the health check probes the app on localhost.
RUN apt-get update \
 && apt-get install --yes --no-install-recommends libpq5 curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /wheels /wheels
RUN pip install --no-index --find-links=/wheels autofix \
 && rm -rf /wheels

# Run as an unprivileged user with a fixed UID for predictable volume ownership.
RUN groupadd --gid 10001 autofix \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin autofix

WORKDIR /app

COPY --chown=autofix:autofix alembic.ini ./
COPY --chown=autofix:autofix alembic ./alembic
COPY --chown=autofix:autofix app ./app
COPY --chown=autofix:autofix scripts ./scripts
COPY --chown=autofix:autofix docker-entrypoint.sh ./docker-entrypoint.sh

# Create the upload directory before startup and grant ownership to the runtime user.
RUN mkdir -p /app/uploads && chown autofix:autofix /app/uploads

# Set execute permission explicitly so Windows checkouts can use the entrypoint.
RUN chmod +x /app/docker-entrypoint.sh

USER autofix

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]