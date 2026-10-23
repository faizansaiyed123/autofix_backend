"""Tests for secure customer signup and email verification."""

from __future__ import annotations

from uuid import UUID
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import routes as auth_routes
from app.core import email as email_module
from app.core.email import dispatch_email_outbox_batch
from app.auth.models import User
from app.customers.models import Customer


def configure_test_mail(monkeypatch: pytest.MonkeyPatch, tokens: dict[str, str]) -> None:
    monkeypatch.setattr(auth_routes, "email_delivery_configured", lambda: True)

    async def capture_message(_email: str, _subject: str, text_body: str, _html_body: str) -> None:
        tokens["body"] = text_body

    monkeypatch.setattr(email_module, "_send", capture_message)


async def deliver_verification(tokens: dict[str, str]) -> None:
    processed = await dispatch_email_outbox_batch()
    assert processed == 1
    link = next(
        line for line in tokens["body"].splitlines()
        if "/verify-email#token=" in line
    )
    tokens["verification"] = parse_qs(urlparse(link).fragment)["token"][0]


class TestCustomerRegistration:
    @pytest.mark.asyncio
    async def test_registration_verifies_email_and_creates_linked_customer(
        self, client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ):
        tokens: dict[str, str] = {}
        configure_test_mail(monkeypatch, tokens)
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "first_name": "New",
                "last_name": "Customer",
                "email": "new.customer@example.com",
                "password": "correct-horse-7",
                "phone": "+1-503-555-0198",
                "is_staff": True,
                "roles": ["OWNER"],
            },
        )
        assert response.status_code == 202
        assert "access_token" not in response.json()
        await deliver_verification(tokens)
        assert tokens["verification"]

        # Correct credentials cannot bypass verification.
        login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": "new.customer@example.com", "password": "correct-horse-7"},
        )
        assert login_response.status_code == 401

        verification = await client.post(
            "/api/v1/auth/verify-email",
            json={"token": tokens["verification"]},
        )
        assert verification.status_code == 200
        data = verification.json()
        assert data["access_token"]
        assert data["refresh_token"]

        headers = {"Authorization": f"Bearer {data['access_token']}"}
        me_response = await client.get("/api/v1/auth/me", headers=headers)
        assert me_response.status_code == 200
        user_data = me_response.json()
        assert user_data["email"] == "new.customer@example.com"
        assert user_data["email_verified"] is True
        assert user_data["is_staff"] is False
        assert user_data["roles"] == ["CUSTOMER"]

        portal_response = await client.get("/api/v1/portal/", headers=headers)
        assert portal_response.status_code == 200
        assert portal_response.json()["email"] == "new.customer@example.com"
        staff_response = await client.get("/api/v1/users/", headers=headers)
        assert staff_response.status_code == 403

        customer_result = await db.execute(
            select(Customer).where(Customer.user_id == UUID(user_data["id"]))
        )
        customer = customer_result.scalar_one()
        assert customer.email == "new.customer@example.com"
        assert customer.first_name == "New"
        assert customer.last_name == "Customer"
        assert customer.phone == "+1-503-555-0198"
        assert customer.user_id == UUID(user_data["id"])

        # The token is single use.
        reused = await client.post(
            "/api/v1/auth/verify-email",
            json={"token": tokens["verification"]},
        )
        assert reused.status_code == 400

    @pytest.mark.asyncio
    async def test_registration_normalizes_email(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        tokens: dict[str, str] = {}
        configure_test_mail(monkeypatch, tokens)
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "first_name": "Email",
                "last_name": "Case",
                "email": "  NEW.CASE@EXAMPLE.COM  ",
                "password": "correct-horse-7",
            },
        )
        assert response.status_code == 202
        await deliver_verification(tokens)
        verified = await client.post(
            "/api/v1/auth/verify-email",
            json={"token": tokens["verification"]},
        )
        assert verified.status_code == 200
        me_response = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {verified.json()['access_token']}"},
        )
        assert me_response.status_code == 200
        assert me_response.json()["email"] == "new.case@example.com"

    @pytest.mark.asyncio
    async def test_registration_rejects_existing_user_without_enumerating(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        tokens: dict[str, str] = {}
        configure_test_mail(monkeypatch, tokens)
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "first_name": "Existing",
                "last_name": "User",
                "email": "OWNER@autofix.demo",
                "password": "correct-horse-7",
            },
        )
        assert response.status_code == 202
        assert tokens == {}

    @pytest.mark.asyncio
    async def test_registration_does_not_claim_existing_customer_record(
        self, client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ):
        tokens: dict[str, str] = {}
        configure_test_mail(monkeypatch, tokens)
        db.add(Customer(
            first_name="Existing",
            last_name="Workshop Customer",
            email="existing.customer@example.com",
            phone="+1-503-555-0100",
        ))
        await db.commit()
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "first_name": "Impersonator",
                "last_name": "Example",
                "email": "existing.customer@example.com",
                "password": "correct-horse-7",
            },
        )
        assert response.status_code == 202
        assert tokens == {}
        result = await db.execute(
            select(User).where(User.email == "existing.customer@example.com")
        )
        assert result.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_registration_requires_email_delivery_configuration(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(auth_routes, "email_delivery_configured", lambda: False)
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "first_name": "No",
                "last_name": "Mailer",
                "email": "no.mailer@example.com",
                "password": "correct-horse-7",
            },
        )
        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_registration_rejects_short_password(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "first_name": "Short",
                "last_name": "Password",
                "email": "short.password@example.com",
                "password": "short",
            },
        )
        assert response.status_code == 422
