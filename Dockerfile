# syntax=docker/dockerfile:1
#
# AutoFix backend.
#
# Two stages, and the split is not about size: asyncpg and bcrypt ship wheels for
# most platforms but not all of them, so pip needs a compiler while it resolves.
# Building in a stage that is then discarded means the runtime image carries no
# toolchain at all — which is the whole point of running as a non-root user,
# since a compiler in the image is a compiler somebody can reach.

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

# pyproject.toml *is* the dependency list, so copying it alone is enough for pip
# to resolve and build the whole tree. Keeping the source out of this layer means
# editing a service does not re-download cryptography.
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

# libpq5 is asyncpg's runtime library. curl is here only so HEALTHCHECK has
# something local to talk to; it talks to 127.0.0.1 rather than the service name
# on purpose, so the check answers "is this process serving?" and not "is the
# network up?".
RUN apt-get update \
 && apt-get install --yes --no-install-recommends libpq5 curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /wheels /wheels
RUN pip install --no-index --find-links=/wheels autofix \
 && rm -rf /wheels

# Unprivileged, with a fixed uid so a bind-mounted volume has predictable
# ownership rather than whatever the host's first user happens to be.
RUN groupadd --gid 10001 autofix \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin autofix

WORKDIR /app

COPY --chown=autofix:autofix alembic.ini ./
COPY --chown=autofix:autofix alembic ./alembic
COPY --chown=autofix:autofix app ./app
COPY --chown=autofix:autofix scripts ./scripts
COPY --chown=autofix:autofix docker-entrypoint.sh ./docker-entrypoint.sh

# Uploads are the one path the app writes to. It exists before the app starts so
# a first run cannot fail on a missing directory, and it belongs to the
# unprivileged user for the same reason everything else does.
RUN mkdir -p /app/uploads && chown autofix:autofix /app/uploads

# chmod rather than relying on the build context: a checkout on Windows has no
# executable bit to preserve, and an entrypoint that cannot run fails at the one
# moment nobody is watching.
RUN chmod +x /app/docker-entrypoint.sh

USER autofix

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]