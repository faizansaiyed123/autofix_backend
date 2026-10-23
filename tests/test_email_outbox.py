"""Tests for token-minimizing durable mail queuing and worker delivery."""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.core import email as email_module
from app.core.email import (
    dispatch_email_outbox_batch,
    queue_password_reset_email,
    queue_verification_email,
)
from app.core.email_models import EmailOutbox


async def create_mail_user(
    db: AsyncSession, *, email: str, verified: bool
) -> User:
    user = User(
        email=email,
        password_hash="test-only-password-hash",
        first_name="Mail",
        last_name="Test",
        is_active=True,
        is_staff=False,
        email_verified=verified,
    )
    db.add(user)
    await db.flush()
    return user


@pytest.mark.asyncio
async def test_outbox_persists_only_user_reference_and_message_type(db: AsyncSession):
    verification_user = await create_mail_user(
        db, email="verification@example.com", verified=False
    )
    reset_user = await create_mail_user(
        db, email="reset@example.com", verified=True
    )
    await queue_verification_email(db, verification_user.id)
    await queue_password_reset_email(db, reset_user.id)
    await db.commit()

    result = await db.execute(
        select(EmailOutbox).order_by(EmailOutbox.message_type.asc())
    )
    messages = list(result.scalars().all())
    assert len(messages) == 2

    verify_job = next(m for m in messages if m.message_type == "VERIFY_EMAIL")
    assert verify_job.user_id == verification_user.id
    assert verify_job.status == "PENDING"
    assert verify_job.attempts == 0

    reset_job = next(m for m in messages if m.message_type == "PASSWORD_RESET")
    assert reset_job.user_id == reset_user.id
    assert reset_job.status == "PENDING"
    assert reset_job.attempts == 0

    # The outbox schema intentionally has no recipient, subject, body, or token columns.
    assert not hasattr(verify_job, "text_body")
    assert not hasattr(verify_job, "html_body")
    assert not hasattr(verify_job, "recipient_email")


@pytest.mark.asyncio
async def test_worker_generates_verification_token_only_when_sending(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user = await create_mail_user(
        db, email="worker@example.com", verified=False
    )
    await queue_verification_email(db, user.id)
    await db.commit()

    deliveries: list[tuple[str, str, str]] = []

    async def fake_send(email: str, subject: str, text: str, _html: str) -> None:
        deliveries.append((email, subject, text))

    monkeypatch.setattr(email_module, "_send", fake_send)
    assert await dispatch_email_outbox_batch() == 1
    assert len(deliveries) == 1
    recipient, subject, body = deliveries[0]
    assert recipient == "worker@example.com"
    assert subject == "Verify your AutoFix account"

    link = next(line for line in body.splitlines() if "/verify-email#token=" in line)
    token = parse_qs(urlparse(link).fragment)["token"][0]
    await db.refresh(user)
    assert user.email_verification_token_hash == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert user.email_verification_expires_at is not None

    result = await db.execute(select(EmailOutbox).where(EmailOutbox.user_id == user.id))
    job = result.scalar_one()
    await db.refresh(job)
    assert job.status == "SENT"
    assert job.attempts == 1


@pytest.mark.asyncio
async def test_worker_generates_password_reset_token_only_when_sending(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user = await create_mail_user(
        db, email="reset-worker@example.com", verified=True
    )
    await queue_password_reset_email(db, user.id)
    await db.commit()

    deliveries: list[tuple[str, str, str]] = []

    async def fake_send(email: str, subject: str, text: str, _html: str) -> None:
        deliveries.append((email, subject, text))

    monkeypatch.setattr(email_module, "_send", fake_send)
    assert await dispatch_email_outbox_batch() == 1
    recipient, subject, body = deliveries[0]
    assert recipient == "reset-worker@example.com"
    assert subject == "Reset your AutoFix password"

    link = next(line for line in body.splitlines() if "/reset-password#token=" in line)
    token = parse_qs(urlparse(link).fragment)["token"][0]
    await db.refresh(user)
    assert user.password_reset_token_hash == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert user.password_reset_expires_at is not None

    result = await db.execute(select(EmailOutbox).where(EmailOutbox.user_id == user.id))
    job = result.scalar_one()
    await db.refresh(job)
    assert job.status == "SENT"
    assert job.attempts == 1


@pytest.mark.asyncio
async def test_worker_retries_delivery_failure_with_backoff(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user = await create_mail_user(
        db, email="retry@example.com", verified=False
    )
    await queue_verification_email(db, user.id)
    await db.commit()

    async def fail_send(_email: str, _subject: str, _text: str, _html: str) -> None:
        raise RuntimeError("fake SMTP failure")

    monkeypatch.setattr(email_module, "_send", fail_send)
    assert await dispatch_email_outbox_batch() == 1

    result = await db.execute(select(EmailOutbox).where(EmailOutbox.user_id == user.id))
    row = result.scalar_one()
    await db.refresh(row)
    assert row.status == "PENDING"
    assert row.attempts == 1
    assert row.last_error == "RuntimeError"
    assert row.available_at > row.created_at
