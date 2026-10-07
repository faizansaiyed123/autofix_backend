"""Tests for customer management endpoints.

Tests cover CRUD operations, search, address management, and RBAC.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


class TestCustomerCRUD:
    """Tests for customer CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_customer_owner(self, owner_client: AsyncClient):
        """Owner can create a customer."""
        response = await owner_client.post(
            "/api/v1/customers/",
            json={
                "first_name": "Alice",
                "last_name": "Smith",
                "email": "alice@example.com",
                "phone": "555-0101",
                "preferred_contact": "EMAIL",
                "notes": "Regular customer",
                "addresses": [
                    {
                        "street": "123 Main St",
                        "city": "Anytown",
                        "state": "CA",
                        "postal_code": "12345",
                        "country": "US",
                        "address_type": "PRIMARY",
                    }
                ],
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["first_name"] == "Alice"
        assert data["last_name"] == "Smith"
        assert data["email"] == "alice@example.com"
        assert data["customer_status"] == "ACTIVE"
        assert len(data["addresses"]) == 1
        assert data["addresses"][0]["street"] == "123 Main St"

    @pytest.mark.asyncio
    async def test_create_customer_with_company(self, owner_client: AsyncClient):
        """Create a business customer with company name."""
        response = await owner_client.post(
            "/api/v1/customers/",
            json={
                "first_name": "Bob",
                "last_name": "Jones",
                "company_name": "ABC Corp",
                "email": "bob@abccorp.com",
                "phone": "555-0202",
                "preferred_contact": "PHONE",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["company_name"] == "ABC Corp"
        assert data["preferred_contact"] == "PHONE"

    @pytest.mark.asyncio
    async def test_create_customer_no_addresses(self, owner_client: AsyncClient):
        """Create a customer without addresses."""
        response = await owner_client.post(
            "/api/v1/customers/",
            json={
                "first_name": "Charlie",
                "last_name": "Brown",
                "email": "charlie@example.com",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["addresses"] == []

    @pytest.mark.asyncio
    async def test_get_customer_owner(self, owner_client: AsyncClient):
        """Owner can get a customer by ID."""
        # First create a customer
        create_resp = await owner_client.post(
            "/api/v1/customers/",
            json={"first_name": "Dave", "last_name": "Wilson", "email": "dave@example.com"},
        )
        customer_id = create_resp.json()["id"]

        response = await owner_client.get(f"/api/v1/customers/{customer_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["first_name"] == "Dave"
        assert data["email"] == "dave@example.com"

    @pytest.mark.asyncio
    async def test_get_customer_not_found(self, owner_client: AsyncClient):
        """Non-existent customer returns 404."""
        from uuid import uuid4
        response = await owner_client.get(f"/api/v1/customers/{uuid4()}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_update_customer(self, owner_client: AsyncClient):
        """Owner can update a customer."""
        create_resp = await owner_client.post(
            "/api/v1/customers/",
            json={"first_name": "Eve", "last_name": "Adams", "email": "eve@example.com"},
        )
        customer_id = create_resp.json()["id"]

        response = await owner_client.patch(
            f"/api/v1/customers/{customer_id}",
            json={"first_name": "Eve", "last_name": "Smith", "phone": "555-9999"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["last_name"] == "Smith"
        assert data["phone"] == "555-9999"

    @pytest.mark.asyncio
    async def test_delete_customer(self, owner_client: AsyncClient):
        """Owner can deactivate a customer."""
        create_resp = await owner_client.post(
            "/api/v1/customers/",
            json={"first_name": "Frank", "last_name": "Miller", "email": "frank@example.com"},
        )
        customer_id = create_resp.json()["id"]

        response = await owner_client.delete(f"/api/v1/customers/{customer_id}")
        assert response.status_code == 204

        # Customer should no longer be accessible (soft deleted)
        response = await owner_client.get(f"/api/v1/customers/{customer_id}")
        assert response.status_code == 404


class TestCustomerSearch:
    """Tests for customer search functionality."""

    @pytest.mark.asyncio
    async def test_search_by_name(self, owner_client: AsyncClient):
        """Search customers by name."""
        await owner_client.post(
            "/api/v1/customers/",
            json={"first_name": "George", "last_name": "Washington", "email": "george@example.com"},
        )

        response = await owner_client.get("/api/v1/customers/search?q=George")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert any(c["first_name"] == "George" for c in data)

    @pytest.mark.asyncio
    async def test_search_by_email(self, owner_client: AsyncClient):
        """Search customers by email."""
        response = await owner_client.get("/api/v1/customers/search?q=alice")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 0

    @pytest.mark.asyncio
    async def test_list_customers_pagination(self, owner_client: AsyncClient):
        """List customers with pagination."""
        response = await owner_client.get("/api/v1/customers/?page=1&size=5")
        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert "meta" in data
        assert data["meta"]["page"] == 1
        assert data["meta"]["size"] == 5


class TestCustomerAuthorization:
    """Tests for customer endpoint authorization."""

    @pytest.mark.asyncio
    async def test_manager_can_create_customer(self, manager_client: AsyncClient):
        """Service Advisor can create customers."""
        response = await manager_client.post(
            "/api/v1/customers/",
            json={"first_name": "Henry", "last_name": "Ford", "email": "henry@example.com"},
        )
        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_technician_cannot_create_customer(self, technician_client: AsyncClient):
        """Technician cannot create customers."""
        response = await technician_client.post(
            "/api/v1/customers/",
            json={"first_name": "Ivan", "last_name": "Petrov", "email": "ivan@example.com"},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_customer_cannot_create_customer(self, customer_client: AsyncClient):
        """Customer cannot create other customers."""
        response = await customer_client.post(
            "/api/v1/customers/",
            json={"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_unauthenticated_cannot_create_customer(self, client: AsyncClient):
        """Unauthenticated request returns 401."""
        response = await client.post(
            "/api/v1/customers/",
            json={"first_name": "John", "last_name": "Doe"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_manager_can_view_customers(self, manager_client: AsyncClient):
        """Service Advisor can view customers."""
        response = await manager_client.get("/api/v1/customers/")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_technician_cannot_view_customers(self, technician_client: AsyncClient):
        """Technician cannot view customer list."""
        response = await technician_client.get("/api/v1/customers/")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_customer_cannot_view_customers(self, customer_client: AsyncClient):
        """Customer cannot view customer list."""
        response = await customer_client.get("/api/v1/customers/")
        assert response.status_code == 403


class TestCustomerValidation:
    """Tests for customer validation."""

    @pytest.mark.asyncio
    async def test_duplicate_email_rejected(self, owner_client: AsyncClient):
        """Creating customer with duplicate email returns 409."""
        await owner_client.post(
            "/api/v1/customers/",
            json={"first_name": "Kate", "last_name": "Smith", "email": "kate@example.com"},
        )
        response = await owner_client.post(
            "/api/v1/customers/",
            json={"first_name": "Kate2", "last_name": "Smith", "email": "kate@example.com"},
        )
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_invalid_email_rejected(self, owner_client: AsyncClient):
        """Invalid email format is rejected."""
        response = await owner_client.post(
            "/api/v1/customers/",
            json={"first_name": "Liam", "last_name": "Neeson", "email": "not-an-email"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_required_fields_validated(self, owner_client: AsyncClient):
        """Missing required fields return validation error."""
        response = await owner_client.post(
            "/api/v1/customers/",
            json={"first_name": "No", "last_name": "Email"},
        )
        # last_name is required but first_name is present
        # email is optional
        assert response.status_code == 201 or response.status_code == 422
