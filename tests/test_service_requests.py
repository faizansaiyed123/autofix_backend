"""Tests for service request management endpoints.

Tests cover CRUD operations, status transitions,
and RBAC authorization.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.customers.models import Customer
from app.vehicles.models import Vehicle
from tests.factories import VehicleFactory


@pytest.fixture()
async def test_customer(db: AsyncSession):
    """Create a test customer for service request association."""
    customer = Customer(
        first_name="SR",
        last_name="Test",
        email="sr_test@example.com",
        phone="555-0001",
        preferred_contact="EMAIL",
        customer_status="ACTIVE",
    )
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    return customer


@pytest.fixture()
async def test_vehicle(db: AsyncSession, test_customer: Customer):
    """Create a test vehicle for service request association."""
    vehicle = VehicleFactory.build(customer_id=test_customer.id)
    db.add(vehicle)
    await db.commit()
    await db.refresh(vehicle)
    return vehicle


class TestServiceRequestCRUD:
    """Tests for service request CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_service_request_owner(self, owner_client: AsyncClient, test_customer: Customer):
        """Owner can create a service request."""
        response = await owner_client.post(
            "/api/v1/service_requests/",
            json={
                "customer_id": str(test_customer.id),
                "title": "Engine making noise",
                "description": "Loud knocking sound when accelerating",
                "priority": "HIGH",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["title"] == "Engine making noise"
        assert data["description"] == "Loud knocking sound when accelerating"
        assert data["priority"] == "HIGH"
        assert data["status"] == "NEW"

    @pytest.mark.asyncio
    async def test_create_service_request_with_vehicle(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Owner can create a service request with a vehicle."""
        response = await owner_client.post(
            "/api/v1/service_requests/",
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "title": "Oil change",
                "priority": "STANDARD",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["vehicle_id"] == str(test_vehicle.id)

    @pytest.mark.asyncio
    async def test_create_service_request_invalid_customer(self, owner_client: AsyncClient):
        """Service request with non-existent customer should fail."""
        response = await owner_client.post(
            "/api/v1/service_requests/",
            json={"customer_id": str(uuid4()), "title": "Test request"},
        )
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_create_service_request_missing_title(self, owner_client: AsyncClient, test_customer: Customer):
        """Title is required."""
        response = await owner_client.post(
            "/api/v1/service_requests/",
            json={"customer_id": str(test_customer.id)},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_get_service_request_owner(self, owner_client: AsyncClient, test_customer: Customer):
        """Owner can get a service request by ID."""
        create_resp = await owner_client.post(
            "/api/v1/service_requests/",
            json={"customer_id": str(test_customer.id), "title": "Brake pads"},
        )
        request_id = create_resp.json()["id"]

        response = await owner_client.get(f"/api/v1/service_requests/{request_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Brake pads"

    @pytest.mark.asyncio
    async def test_get_service_request_not_found(self, owner_client: AsyncClient):
        """Non-existent service request returns 404."""
        response = await owner_client.get(f"/api/v1/service_requests/{uuid4()}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_update_service_request(self, owner_client: AsyncClient, test_customer: Customer):
        """Owner can update a service request."""
        create_resp = await owner_client.post(
            "/api/v1/service_requests/",
            json={"customer_id": str(test_customer.id), "title": "Old title"},
        )
        request_id = create_resp.json()["id"]

        response = await owner_client.patch(
            f"/api/v1/service_requests/{request_id}",
            json={"title": "New title", "priority": "LOW"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "New title"
        assert data["priority"] == "LOW"


class TestServiceRequestStatus:
    """Tests for status transition logic."""

    @pytest.mark.asyncio
    async def test_status_transition_new_to_in_review(self, owner_client: AsyncClient, test_customer: Customer):
        """Transition from NEW to IN_REVIEW is allowed."""
        create_resp = await owner_client.post(
            "/api/v1/service_requests/",
            json={"customer_id": str(test_customer.id), "title": "Test"},
        )
        request_id = create_resp.json()["id"]

        response = await owner_client.patch(
            f"/api/v1/service_requests/{request_id}/status",
            params={"status": "IN_REVIEW"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "IN_REVIEW"

    @pytest.mark.asyncio
    async def test_status_transition_new_to_approved_invalid(self, owner_client: AsyncClient, test_customer: Customer):
        """Transition from NEW to APPROVED is not allowed (must go through IN_REVIEW)."""
        create_resp = await owner_client.post(
            "/api/v1/service_requests/",
            json={"customer_id": str(test_customer.id), "title": "Test"},
        )
        request_id = create_resp.json()["id"]

        response = await owner_client.patch(
            f"/api/v1/service_requests/{request_id}/status",
            params={"status": "APPROVED"},
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_status_transition_invalid_status(self, owner_client: AsyncClient, test_customer: Customer):
        """Invalid status value returns 400."""
        create_resp = await owner_client.post(
            "/api/v1/service_requests/",
            json={"customer_id": str(test_customer.id), "title": "Test"},
        )
        request_id = create_resp.json()["id"]

        response = await owner_client.patch(
            f"/api/v1/service_requests/{request_id}/status",
            params={"status": "INVALID"},
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_status_transition_new_to_rejected(self, owner_client: AsyncClient, test_customer: Customer):
        """Transition from NEW to REJECTED is allowed."""
        create_resp = await owner_client.post(
            "/api/v1/service_requests/",
            json={"customer_id": str(test_customer.id), "title": "Test"},
        )
        request_id = create_resp.json()["id"]

        response = await owner_client.patch(
            f"/api/v1/service_requests/{request_id}/status",
            params={"status": "REJECTED"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "REJECTED"


class TestServiceRequestAuthorization:
    """RBAC tests for service request endpoints."""

    @pytest.mark.asyncio
    async def test_manager_can_create_request(self, manager_client: AsyncClient, test_customer: Customer):
        """Service Advisor can create service requests."""
        response = await manager_client.post(
            "/api/v1/service_requests/",
            json={"customer_id": str(test_customer.id), "title": "Test"},
        )
        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_manager_can_read_requests(self, manager_client: AsyncClient, test_customer: Customer):
        """Service Advisor can list service requests."""
        response = await manager_client.get("/api/v1/service_requests/")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_technician_can_read_requests(self, technician_client: AsyncClient):
        """Technician can read service requests."""
        response = await technician_client.get("/api/v1/service_requests/")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_technician_cannot_create_request(self, technician_client: AsyncClient, test_customer: Customer):
        """Technician cannot create service requests."""
        response = await technician_client.post(
            "/api/v1/service_requests/",
            json={"customer_id": str(test_customer.id), "title": "Test"},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_customer_cannot_read_the_shop_request_queue(
        self, customer_client: AsyncClient
    ):
        """The customer holds service_requests:read, and is still refused here.

        This is the shop's queue: every request from every customer. The
        customer's own requests are in the portal, filtered by the account the
        token belongs to.
        """
        response = await customer_client.get("/api/v1/service_requests/")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_customer_cannot_file_a_request_through_the_shop_api(
        self, customer_client: AsyncClient, test_customer: Customer
    ):
        """Filing a request needs a customer id, and here the caller supplies it.

        That is the whole reason this is refused rather than merely untidy: the
        body carries ``customer_id``, so a customer who could post here could file
        a request against somebody else's account. The portal has no such field —
        it derives the customer from the token — and is where a customer files.
        """
        response = await customer_client.post(
            "/api/v1/service_requests/",
            json={"customer_id": str(test_customer.id), "title": "My car needs service"},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_parts_staff_cannot_create_request(self, parts_client: AsyncClient, test_customer: Customer):
        """Parts staff cannot create service requests."""
        response = await parts_client.post(
            "/api/v1/service_requests/",
            json={"customer_id": str(test_customer.id), "title": "Test"},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_unauthenticated_cannot_access(self, unauth_client: AsyncClient):
        """Unauthenticated requests are rejected."""
        response = await unauth_client.get("/api/v1/service_requests/")
        assert response.status_code == 401
