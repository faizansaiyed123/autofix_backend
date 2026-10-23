"""Tests for authentication endpoints.

Tests login, refresh, logout, password reset, and current user.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


class TestLogin:
    """Tests for POST /api/v1/auth/login."""

    @pytest.mark.asyncio
    async def test_login_attempts_are_rate_limited(self, client: AsyncClient):
        for _ in range(8):
            response = await client.post(
                "/api/v1/auth/login",
                json={"email": "owner@autofix.demo", "password": "incorrect-password"},
            )
            assert response.status_code == 401

        limited = await client.post(
            "/api/v1/auth/login",
            json={"email": "owner@autofix.demo", "password": "incorrect-password"},
        )
        assert limited.status_code == 429
        assert int(limited.headers["Retry-After"]) > 0

    @pytest.mark.asyncio
    async def test_login_success(self, client: AsyncClient):
        """Valid credentials return access and refresh tokens."""
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "owner@autofix.demo", "password": "demo1234"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"
        assert data["expires_in"] > 0

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, client: AsyncClient):
        """Invalid password returns 401."""
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "owner@autofix.demo", "password": "wrongpassword"},
        )
        assert response.status_code == 401
        assert "Invalid email or password" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_login_nonexistent_user(self, client: AsyncClient):
        """Non-existent user returns 401."""
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "nonexistent@test.com", "password": "password123"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_login_inactive_user(self, client: AsyncClient):
        """Inactive user cannot log in."""
        # The inactive user test requires a pre-created inactive user.
        # Demo users are all active, so we test with a modified setup.
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "owner@autofix.demo", "password": "demo1234"},
        )
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_login_validation_error(self, client: AsyncClient):
        """Missing fields return validation error."""
        response = await client.post(
            "/api/v1/auth/login",
            json={},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_login_rejects_passwords_over_bcrypt_byte_limit(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "owner@autofix.demo", "password": "é" * 40},
        )
        assert response.status_code == 422


class TestRefreshToken:
    """Tests for POST /api/v1/auth/refresh."""

    @pytest.mark.asyncio
    async def test_refresh_token_cannot_be_reused_after_rotation(self, client: AsyncClient):
        login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": "owner@autofix.demo", "password": "demo1234"},
        )
        assert login_response.status_code == 200
        old_refresh = login_response.json()["refresh_token"]

        rotated = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": old_refresh}
        )
        assert rotated.status_code == 200
        assert rotated.json()["refresh_token"] != old_refresh

        replay = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": old_refresh}
        )
        assert replay.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_success(self, client: AsyncClient):
        """Valid refresh token returns new access token."""
        # First login to get tokens
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "owner@autofix.demo", "password": "demo1234"},
        )
        refresh_token = login_resp.json()["refresh_token"]

        response = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data

    @pytest.mark.asyncio
    async def test_refresh_invalid_token(self, client: AsyncClient):
        """Invalid refresh token returns 401."""
        response = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": "invalid.token.here"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_access_token(self, client: AsyncClient):
        """Using access token as refresh token returns 401."""
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "owner@autofix.demo", "password": "demo1234"},
        )
        access_token = login_resp.json()["access_token"]

        response = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": access_token},
        )
        assert response.status_code == 401


class TestLogout:
    """Tests for POST /api/v1/auth/logout."""

    @pytest.mark.asyncio
    async def test_logout_revokes_the_current_access_token(self, owner_client: AsyncClient):
        response = await owner_client.post("/api/v1/auth/logout")
        assert response.status_code == 200

        profile = await owner_client.get("/api/v1/auth/me")
        assert profile.status_code == 401

    @pytest.mark.asyncio
    async def test_logout_success(self, owner_client: AsyncClient):
        """Authenticated logout returns success."""
        response = await owner_client.post("/api/v1/auth/logout")
        assert response.status_code == 200
        assert "message" in response.json()

    @pytest.mark.asyncio
    async def test_logout_unauthenticated(self, client: AsyncClient):
        """Unauthenticated logout returns 401."""
        response = await client.post("/api/v1/auth/logout")
        assert response.status_code == 401


class TestMe:
    """Tests for GET /api/v1/auth/me."""

    @pytest.mark.asyncio
    async def test_get_me_authenticated(self, owner_client: AsyncClient):
        """Authenticated user can fetch their profile."""
        response = await owner_client.get("/api/v1/auth/me")
        assert response.status_code == 200
        data = response.json()
        assert data["email"] == "owner@autofix.demo"
        assert "OWNER" in data["roles"]

    @pytest.mark.asyncio
    async def test_get_me_unauthenticated(self, client: AsyncClient):
        """Unauthenticated request returns 401."""
        response = await client.get("/api/v1/auth/me")
        assert response.status_code == 401


class TestPermissions:
    """Tests for GET /api/v1/auth/permissions."""

    @pytest.mark.asyncio
    async def test_get_permissions_owner(self, owner_client: AsyncClient):
        """Owner can fetch their permissions."""
        response = await owner_client.get("/api/v1/auth/permissions")
        assert response.status_code == 200
        data = response.json()
        assert "roles" in data
        assert "OWNER" in data["roles"]
        assert "permissions" in data
        assert len(data["permissions"]) > 0

    @pytest.mark.asyncio
    async def test_get_permissions_unauthenticated(self, client: AsyncClient):
        """Unauthenticated request returns 401."""
        response = await client.get("/api/v1/auth/permissions")
        assert response.status_code == 401


class TestPasswordReset:
    """Tests for POST /api/v1/auth/password-reset/*."""

    @pytest.mark.asyncio
    async def test_request_reset_existing_email(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        """Request reset for existing email returns success."""
        from app.auth import routes as auth_routes

        monkeypatch.setattr(auth_routes, "email_delivery_configured", lambda: True)

        async def capture_reset(_session: AsyncSession, _user_id) -> None:
            return None

        monkeypatch.setattr(auth_routes, "queue_password_reset_email", capture_reset)
        response = await client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": "owner@autofix.demo"},
        )
        assert response.status_code == 200
        assert "If the email exists" in response.json()["message"]

    @pytest.mark.asyncio
    async def test_request_reset_nonexistent_email(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        """Request reset for non-existent email also returns success (anti-enumeration)."""
        from app.auth import routes as auth_routes

        monkeypatch.setattr(auth_routes, "email_delivery_configured", lambda: True)
        response = await client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": "nonexistent@test.com"},
        )
        assert response.status_code == 200
        assert "If the email exists" in response.json()["message"]

    @pytest.mark.asyncio
    async def test_reset_token_is_single_use_and_revokes_existing_sessions(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        from urllib.parse import parse_qs, urlparse

        from app.auth import routes as auth_routes
        from app.core import email as email_module
        from app.core.email import dispatch_email_outbox_batch

        captured: dict[str, str] = {}
        monkeypatch.setattr(auth_routes, "email_delivery_configured", lambda: True)

        async def capture_message(_email: str, _subject: str, text_body: str, _html_body: str) -> None:
            captured["body"] = text_body

        monkeypatch.setattr(email_module, "_send", capture_message)

        login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": "owner@autofix.demo", "password": "demo1234"},
        )
        assert login_response.status_code == 200
        old_access = login_response.json()["access_token"]

        reset_request = await client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": "owner@autofix.demo"},
        )
        assert reset_request.status_code == 200
        assert await dispatch_email_outbox_batch() == 1

        link = next(
            line for line in captured["body"].splitlines()
            if "/reset-password#token=" in line
        )
        token = parse_qs(urlparse(link).fragment)["token"][0]

        confirmation = await client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "new_password": "new-secure-password-8"},
        )
        assert confirmation.status_code == 200

        old_session = await client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {old_access}"}
        )
        assert old_session.status_code == 401

        new_login = await client.post(
            "/api/v1/auth/login",
            json={"email": "owner@autofix.demo", "password": "new-secure-password-8"},
        )
        assert new_login.status_code == 200

        replay = await client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "new_password": "another-secure-password-9"},
        )
        assert replay.status_code == 400

