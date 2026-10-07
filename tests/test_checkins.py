"""Tests for check-in management endpoints.

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
    """Create a test customer."""
    customer = Customer(
        first_name="Check",
        last_name="Customer",
        email="checkin_test@example.com",
        phone="555-0002",
        preferred_contact="EMAIL",
        customer_status="ACTIVE",
    )
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    return customer


@pytest.fixture()
async def test_vehicle(db: AsyncSession, test_customer: Customer):
    """Create a test vehicle."""
    vehicle = VehicleFactory.build(customer_id=test_customer.id)
    db.add(vehicle)
    await db.commit()
    await db.refresh(vehicle)
    return vehicle


class TestCheckInCRUD:
    """Tests for check-in CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_checkin_owner(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Owner can create a check-in."""
        response = await owner_client.post(
            "/api/v1/check_ins/",
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "odometer": 45000,
                "checkin_type": "APPOINTMENT",
                "notes": "Regular maintenance",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["odometer"] == 45000
        assert data["checkin_type"] == "APPOINTMENT"
        assert data["status"] == "PENDING"
        assert data["notes"] == "Regular maintenance"

    @pytest.mark.asyncio
    async def test_create_checkin_missing_odometer(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Odometer is required."""
        response = await owner_client.post(
            "/api/v1/check_ins/",
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_checkin_invalid_customer(self, owner_client: AsyncClient, test_vehicle: Vehicle):
        """Check-in with non-existent customer should fail."""
        response = await owner_client.post(
            "/api/v1/check_ins/",
            json={
                "customer_id": str(uuid4()),
                "vehicle_id": str(test_vehicle.id),
                "odometer": 50000,
            },
        )
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_get_checkin_owner(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Owner can get a check-in by ID."""
        create_resp = await owner_client.post(
            "/api/v1/check_ins/",
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "odometer": 50000,
            },
        )
        checkin_id = create_resp.json()["id"]

        response = await owner_client.get(f"/api/v1/check_ins/{checkin_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["odometer"] == 50000

    @pytest.mark.asyncio
    async def test_get_checkin_not_found(self, owner_client: AsyncClient):
        """Non-existent check-in returns 404."""
        response = await owner_client.get(f"/api/v1/check_ins/{uuid4()}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_update_checkin(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Owner can update a check-in."""
        create_resp = await owner_client.post(
            "/api/v1/check_ins/",
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "odometer": 40000,
            },
        )
        checkin_id = create_resp.json()["id"]

        response = await owner_client.patch(
            f"/api/v1/check_ins/{checkin_id}",
            json={"notes": "Updated notes", "tire_condition": "Good"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["notes"] == "Updated notes"
        assert data["tire_condition"] == "Good"

    @pytest.mark.asyncio
    async def test_list_checkins(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Owner can list check-ins."""
        await owner_client.post(
            "/api/v1/check_ins/",
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "odometer": 40000,
            },
        )
        response = await owner_client.get("/api/v1/check_ins/")
        assert response.status_code == 200
        data = response.json()
        assert data["meta"]["total"] >= 1


class TestCheckInStatus:
    """Tests for check-in status transitions."""

    @pytest.mark.asyncio
    async def test_status_transition_pending_to_in_progress(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Transition from PENDING to IN_PROGRESS is allowed."""
        create_resp = await owner_client.post(
            "/api/v1/check_ins/",
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "odometer": 40000,
            },
        )
        checkin_id = create_resp.json()["id"]

        response = await owner_client.patch(
            f"/api/v1/check_ins/{checkin_id}/status",
            params={"status": "IN_PROGRESS"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "IN_PROGRESS"

    @pytest.mark.asyncio
    async def test_status_transition_in_progress_to_completed(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Transition from IN_PROGRESS to COMPLETED is allowed."""
        create_resp = await owner_client.post(
            "/api/v1/check_ins/",
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "odometer": 40000,
            },
        )
        checkin_id = create_resp.json()["id"]

        await owner_client.patch(
            f"/api/v1/check_ins/{checkin_id}/status",
            params={"status": "IN_PROGRESS"},
        )

        response = await owner_client.patch(
            f"/api/v1/check_ins/{checkin_id}/status",
            params={"status": "COMPLETED"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "COMPLETED"

    @pytest.mark.asyncio
    async def test_status_transition_pending_to_completed_invalid(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Transition from PENDING to COMPLETED is not allowed (must go through IN_PROGRESS)."""
        create_resp = await owner_client.post(
            "/api/v1/check_ins/",
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "odometer": 40000,
            },
        )
        checkin_id = create_resp.json()["id"]

        response = await owner_client.patch(
            f"/api/v1/check_ins/{checkin_id}/status",
            params={"status": "COMPLETED"},
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_status_transition_invalid_status(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Invalid status value returns 400."""
        create_resp = await owner_client.post(
            "/api/v1/check_ins/",
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "odometer": 40000,
            },
        )
        checkin_id = create_resp.json()["id"]

        response = await owner_client.patch(
            f"/api/v1/check_ins/{checkin_id}/status",
            params={"status": "INVALID_STATUS"},
        )
        assert response.status_code == 400


class TestCheckInAuthorization:
    """RBAC tests for check-in endpoints."""

    @pytest.mark.asyncio
    async def test_manager_can_create_checkin(self, manager_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Service Advisor can create check-ins."""
        response = await manager_client.post(
            "/api/v1/check_ins/",
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "odometer": 30000,
            },
        )
        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_technician_can_read_checkins(self, technician_client: AsyncClient):
        """Technician can read check-ins."""
        response = await technician_client.get("/api/v1/check_ins/")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_technician_cannot_write_checkin(self, technician_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Technician cannot create check-ins (read-only)."""
        response = await technician_client.post(
            "/api/v1/check_ins/",
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "odometer": 30000,
            },
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_parts_staff_cannot_access(self, parts_client: AsyncClient):
        """Parts staff cannot access check-ins."""
        response = await parts_client.get("/api/v1/check_ins/")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_unauthenticated_cannot_access(self, unauth_client: AsyncClient):
        """Unauthenticated requests are rejected."""
        response = await unauth_client.get("/api/v1/check_ins/")
        assert response.status_code == 401
