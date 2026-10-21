#!/bin/sh
# Bring the schema up to date, optionally seed demo data, then start the server.
# A failed migration exits before the application can serve requests.
set -e

echo "==> Waiting for the database"
# Retry while PostgreSQL completes startup.
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

# Demo data is opt-in; leave it disabled for deployments with real customer data.
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