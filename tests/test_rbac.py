"""Tests for RBAC and authorization.

Tests that each role has correct permissions and that endpoints
are properly protected by role/permission checks.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


class TestUserRolePermissions:
    """Verify role-to-permission mappings in the database."""

    @pytest.mark.asyncio
    async def test_owner_has_all_permissions(self, owner_client: AsyncClient):
        """Owner should have all permissions."""
        response = await owner_client.get("/api/v1/auth/permissions")
        assert response.status_code == 200
        perms = response.json()["permissions"]
        # Owner should have a large number of permissions
        assert len(perms) > 40

    @pytest.mark.asyncio
    async def test_customer_has_restricted_permissions(self, customer_client: AsyncClient):
        """Customer should not have admin permissions."""
        response = await customer_client.get("/api/v1/auth/permissions")
        assert response.status_code == 200
        perms = response.json()["permissions"]
        assert "users:read" not in perms
        assert "users:write" not in perms
        assert "invoices:manage" not in perms
        assert "vehicles:read" in perms

    @pytest.mark.asyncio
    async def test_technician_has_restricted_permissions(self, technician_client: AsyncClient):
        """Technician should not have financial permissions."""
        response = await technician_client.get("/api/v1/auth/permissions")
        assert response.status_code == 200
        perms = response.json()["permissions"]
        assert "invoices:write" not in perms
        assert "invoices:manage" not in perms
        assert "users:manage" not in perms
        assert "repair_orders:read" in perms
        assert "inspections:write" in perms

    @pytest.mark.asyncio
    async def test_parts_staff_has_inventory_permissions(self, parts_client: AsyncClient):
        """Parts staff should have inventory permissions."""
        response = await parts_client.get("/api/v1/auth/permissions")
        assert response.status_code == 200
        perms = response.json()["permissions"]
        assert "inventory:read" in perms
        assert "inventory:write" in perms
        assert "parts:manage" in perms
        assert "invoices:manage" not in perms


class TestUserEndpointAuthorization:
    """Verify user management endpoints are properly protected."""

    @pytest.mark.asyncio
    async def test_owner_can_list_users(self, owner_client: AsyncClient):
        """Owner can list all users."""
        response = await owner_client.get("/api/v1/users/")
        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert len(data["data"]) >= 5

    @pytest.mark.asyncio
    async def test_manager_can_list_users(self, manager_client: AsyncClient):
        """Service Advisor can list users."""
        response = await manager_client.get("/api/v1/users/")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_technician_cannot_list_users(self, technician_client: AsyncClient):
        """Technician cannot list users."""
        response = await technician_client.get("/api/v1/users/")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_customer_cannot_list_users(self, customer_client: AsyncClient):
        """Customer cannot list users."""
        response = await customer_client.get("/api/v1/users/")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_unauthenticated_cannot_list_users(self, client: AsyncClient):
        """Unauthenticated request returns 401."""
        response = await client.get("/api/v1/users/")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_owner_can_create_user(self, owner_client: AsyncClient):
        """Owner can create new users."""
        response = await owner_client.post(
            "/api/v1/users/",
            json={
                "email": "testuser@test.com",
                "first_name": "Test",
                "last_name": "User",
                "password": "testpass123",
                "roles": ["TECHNICIAN"],
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["email"] == "testuser@test.com"
        assert "TECHNICIAN" in data["roles"]

    @pytest.mark.asyncio
    async def test_technician_cannot_create_user(self, technician_client: AsyncClient):
        """Technician cannot create users."""
        response = await technician_client.post(
            "/api/v1/users/",
            json={
                "email": "hacker@test.com",
                "first_name": "Hacker",
                "last_name": "User",
                "password": "testpass123",
                "roles": ["OWNER"],
            },
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_customer_cannot_create_user(self, customer_client: AsyncClient):
        """Customer cannot create users."""
        response = await customer_client.post(
            "/api/v1/users/",
            json={
                "email": "hacker@test.com",
                "first_name": "Hacker",
                "last_name": "User",
                "password": "testpass123",
            },
        )
        assert response.status_code == 403


class TestPermissionValidation:
    """Test that invalid permission assignments are rejected."""

    @pytest.mark.asyncio
    async def test_create_user_invalid_role(self, owner_client: AsyncClient):
        """Creating user with invalid role name returns error."""
        response = await owner_client.post(
            "/api/v1/users/",
            json={
                "email": "badrole@test.com",
                "first_name": "Bad",
                "last_name": "Role",
                "password": "testpass123",
                "roles": ["INVALID_ROLE"],
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_user_duplicate_email(self, owner_client: AsyncClient):
        """Creating user with existing email returns 409."""
        response = await owner_client.post(
            "/api/v1/users/",
            json={
                "email": "owner@autofix.demo",
                "first_name": "Duplicate",
                "last_name": "User",
                "password": "testpass123",
            },
        )
        assert response.status_code == 409
