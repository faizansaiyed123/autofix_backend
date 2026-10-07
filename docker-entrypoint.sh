#!/bin/sh
#
# Container entrypoint: bring the schema up to date, optionally seed, then serve.
#
# Migrations run here rather than in a documented manual step because a container
# that starts successfully against an empty database and then 500s on the first
# query is the worst of both outcomes: it looks healthy to an orchestrator and it
# is not. `alembic upgrade head` is idempotent, so this is safe to repeat.
#
# Anything that exits non-zero stops the container, which is the behaviour a
# deployment needs: a failed migration should be a loud dead pod, not a running
# process serving an out-of-date schema.
set -e

echo "==> Waiting for the database"
# Postgres accepts connections a moment before it finishes its own startup, so
# the first attempt is expected to fail. Retrying here is simpler and more
# honest than asking the application to handle a dead database on every request.
python - <<'PY'
import asyncio
import sys

from app.core.database import close_engine, init_db

DELAY = 1.0

async def main() -> int:
    last = None
    for attempt in range(1, 61):
        try:
            await init_db()
        except Exception as exc:  # noqa: BLE001 - any failure means "not yet"
            last = exc
            print(f"    database not ready ({exc.__class__.__name__}), retrying")
            await asyncio.sleep(DELAY)
        else:
            print(f"    database ready after {attempt} attempt(s)")
            await close_engine()
            return 0
    print(f"    gave up after 60 attempts: {last}", file=sys.stderr)
    return 1

sys.exit(asyncio.run(main()))
PY

echo "==> Applying migrations"
alembic upgrade head

# Off by default. A demo shop in production is worse than an empty one, so the
# flag has to be asked for explicitly; `--no-demo` gives logins without the
# business data for a first run that wants only the five accounts.
if [ "${AUTOFIX_SEED:-0}" = "1" ]; then
    echo "==> Seeding roles, logins and demo data"
    python scripts/seed.py
fi

echo "==> Starting the API on ${APP_HOST:-0.0.0.0}:${APP_PORT:-8000}"
exec uvicorn app.main:app \
    --host "${APP_HOST:-0.0.0.0}" \
    --port "${APP_PORT:-8000}" \
    --proxy-headers \
    --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"