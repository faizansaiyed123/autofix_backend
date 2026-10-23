"""Concurrency tests for the shared PostgreSQL auth rate limiter."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rate_limit import enforce_rate_limit


@pytest.mark.asyncio
async def test_rate_limit_is_atomic_across_independent_sessions(
    db: AsyncSession, test_session_factory
):
    async def attempt() -> bool:
        async with test_session_factory() as session:
            try:
                await enforce_rate_limit(
                    session,
                    scope="concurrency-test-email",
                    identity="single-bucket@example.test",
                    limit=5,
                    window_seconds=600,
                )
                return True
            except HTTPException as exc:
                if exc.status_code == 429:
                    return False
                raise

    outcomes = await asyncio.gather(*(attempt() for _ in range(16)))
    assert sum(outcomes) == 5
    assert outcomes.count(False) == 11
