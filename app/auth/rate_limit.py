"""Database-backed authentication throttling shared by all application workers."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request, status
from sqlalchemy import case, delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AuthRateLimit
from app.core.config import settings


def client_address(request: Request) -> str:
    """Resolve client IP from the hosting edge only when proxy headers are explicitly trusted."""
    if settings.AUTH_RATE_LIMIT_TRUST_PROXY_HEADERS:
        # Render routes public traffic through Cloudflare, which overwrites this edge header.
        # Do not trust arbitrary X-Forwarded-For values: clients can forge the leftmost item.
        forwarded = request.headers.get("cf-connecting-ip", "")
        try:
            return str(ipaddress.ip_address(forwarded.strip()))
        except ValueError:
            pass

    host = request.client.host if request.client else "unknown"
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return "unknown"


async def enforce_rate_limit(
    session: AsyncSession,
    *,
    scope: str,
    identity: str,
    limit: int,
    window_seconds: int,
) -> None:
    """Atomically consume a shared fixed-window counter in PostgreSQL."""
    normalized = identity.strip().casefold() if scope.endswith("-email") else identity.strip()
    key_hash = hmac.new(settings.SECRET_KEY.encode("utf-8"), f"{scope}:{normalized}".encode("utf-8"), hashlib.sha256).hexdigest()
    now = datetime.now(UTC)
    reset_before = now - timedelta(seconds=window_seconds)
    expired = AuthRateLimit.window_started_at <= reset_before

    statement = (
        insert(AuthRateLimit)
        .values(key_hash=key_hash, window_started_at=now, attempt_count=1, last_seen_at=now)
        .on_conflict_do_update(
            index_elements=[AuthRateLimit.key_hash],
            set_={
                "window_started_at": case((expired, now), else_=AuthRateLimit.window_started_at),
                "attempt_count": case((expired, 1), else_=AuthRateLimit.attempt_count + 1),
                "last_seen_at": now,
            },
        )
        .returning(AuthRateLimit.attempt_count, AuthRateLimit.window_started_at)
    )
    result = await session.execute(statement)
    attempts, window_started_at = result.one()
    await session.commit()

    if secrets.randbelow(64) == 0:
        await session.execute(
            delete(AuthRateLimit).where(AuthRateLimit.last_seen_at < now - timedelta(days=7))
        )
        await session.commit()

    if attempts > limit:
        elapsed = max(0, int((now - window_started_at).total_seconds()))
        retry_after = max(1, window_seconds - elapsed)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please wait before trying again.",
            headers={"Retry-After": str(retry_after)},
        )
