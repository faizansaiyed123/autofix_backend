"""Tests for vehicle management endpoints.

Tests cover CRUD operations, VIN validation, mileage tracking,
and RBAC authorization.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.customers.models import Customer
from tests.factories import VehicleFactory


@pytest.fixture()
async def test_customer(db: AsyncSession):
    """Create a test customer for vehicle association."""
    customer = Customer(
        first_name="Test",
        last_name="Customer",
        email="vehicletest@example.com",
        phone="555-0001",
        preferred_contact="EMAIL",
        customer_status="ACTIVE",
    )
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    return customer


class TestVehicleCRUD:
    """Tests for vehicle CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_vehicle_owner(self, owner_client: AsyncClient, test_customer: Customer):
        """Owner can create a vehicle."""
        response = await owner_client.post(
            "/api/v1/vehicles/",
            json={
                "customer_id": str(test_customer.id),
                "vin": "1HGBH41JXMN109186",
                "license_plate": "ABC-123",
                "make": "Toyota",
                "model": "Camry",
                "year": 2020,
                "color": "Silver",
                "mileage": 45000,
                "fuel_type": "GASOLINE",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["make"] == "Toyota"
        assert data["model"] == "Camry"
        assert data["vin"] == "1HGBH41JXMN109186"
        assert data["year"] == 2020
        assert data["mileage"] == 45000
        assert data["status"] == "ACTIVE"

    @pytest.mark.asyncio
    async def test_create_vehicle_invalid_vin(self, owner_client: AsyncClient, test_customer: Customer):
        """Invalid VIN format should be rejected."""
        response = await owner_client.post(
            "/api/v1/vehicles/",
            json={
                "customer_id": str(test_customer.id),
                "vin": "IIIIIIIIIIIIIII",
                "make": "Toyota",
                "model": "Camry",
            },
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_create_vehicle_invalid_customer(self, owner_client: AsyncClient):
        """Vehicle with non-existent customer should fail."""
        response = await owner_client.post(
            "/api/v1/vehicles/",
            json={
                "customer_id": str(uuid4()),
                "make": "Toyota",
                "model": "Camry",
            },
        )
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_get_vehicle_owner(self, owner_client: AsyncClient, test_customer: Customer, db: AsyncSession):
        """Owner can get a vehicle by ID."""
        vehicle = VehicleFactory.build(customer_id=test_customer.id)
        db.add(vehicle)
        await db.commit()
        await db.refresh(vehicle)

        response = await owner_client.get(f"/api/v1/vehicles/{vehicle.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["make"] == vehicle.make
        assert data["model"] == vehicle.model
        assert data["vin"] == vehicle.vin

    @pytest.mark.asyncio
    async def test_get_vehicle_not_found(self, owner_client: AsyncClient):
        """Non-existent vehicle returns 404."""
        response = await owner_client.get(f"/api/v1/vehicles/{uuid4()}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_update_vehicle(self, owner_client: AsyncClient, test_customer: Customer, db: AsyncSession):
        """Owner can update a vehicle."""
        vehicle = VehicleFactory.build(customer_id=test_customer.id)
        db.add(vehicle)
        await db.commit()
        await db.refresh(vehicle)

        response = await owner_client.patch(
            f"/api/v1/vehicles/{vehicle.id}",
            json={"mileage": 50000, "color": "Blue", "make": "Honda"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["mileage"] == 50000
        assert data["color"] == "Blue"
        assert data["make"] == "Honda"

    @pytest.mark.asyncio
    async def test_delete_vehicle(self, owner_client: AsyncClient, test_customer: Customer, db: AsyncSession):
        """Owner can delete a vehicle."""
        vehicle = VehicleFactory.build(customer_id=test_customer.id)
        db.add(vehicle)
        await db.commit()
        await db.refresh(vehicle)

        response = await owner_client.delete(f"/api/v1/vehicles/{vehicle.id}")
        assert response.status_code == 204

        # Verify it's gone
        response = await owner_client.get(f"/api/v1/vehicles/{vehicle.id}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_list_vehicles(self, owner_client: AsyncClient, test_customer: Customer, db: AsyncSession):
        """Owner can list vehicles."""
        v1 = VehicleFactory.build(customer_id=test_customer.id, make="Toyota")
        v2 = VehicleFactory.build(customer_id=test_customer.id, make="Honda")
        db.add_all([v1, v2])
        await db.commit()

        response = await owner_client.get("/api/v1/vehicles/")
        assert response.status_code == 200
        data = response.json()
        assert data["meta"]["total"] >= 2

    @pytest.mark.asyncio
    async def test_list_vehicles_by_customer(self, owner_client: AsyncClient, db: AsyncSession):
        """List vehicles filtered by customer."""
        c1 = Customer(first_name="A", last_name="A", preferred_contact="EMAIL", customer_status="ACTIVE")
        c2 = Customer(first_name="B", last_name="B", preferred_contact="EMAIL", customer_status="ACTIVE")
        db.add_all([c1, c2])
        await db.commit()
        await db.refresh(c1)
        await db.refresh(c2)

        v1 = VehicleFactory.build(customer_id=c1.id, make="Toyota")
        v2 = VehicleFactory.build(customer_id=c2.id, make="Honda")
        db.add_all([v1, v2])
        await db.commit()

        response = await owner_client.get(f"/api/v1/vehicles/?customer_id={c1.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["meta"]["total"] == 1
        assert data["data"][0]["make"] == "Toyota"

    @pytest.mark.asyncio
    async def test_search_vehicles(self, owner_client: AsyncClient, test_customer: Customer, db: AsyncSession):
        """Search vehicles by make/model/plate."""
        vehicle = VehicleFactory.build(customer_id=test_customer.id, make="Toyota", model="Camry", license_plate="ABC-123")
        db.add(vehicle)
        await db.commit()

        response = await owner_client.get("/api/v1/vehicles/search?q=Toyota")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert data[0]["make"] == "Toyota"


class TestVehicleMileage:
    """Tests for mileage record operations."""

    @pytest.mark.asyncio
    async def test_add_mileage_record(self, owner_client: AsyncClient, test_customer: Customer, db: AsyncSession):
        """Add a mileage record updates vehicle mileage."""
        vehicle = VehicleFactory.build(customer_id=test_customer.id, mileage=30000)
        db.add(vehicle)
        await db.commit()
        await db.refresh(vehicle)

        response = await owner_client.post(
            f"/api/v1/vehicles/{vehicle.id}/mileage",
            json={"mileage": 35000, "source": "MANUAL", "notes": "Oil change"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["mileage"] == 35000
        assert data["source"] == "MANUAL"
        assert data["notes"] == "Oil change"

        # Verify vehicle mileage was updated
        response = await owner_client.get(f"/api/v1/vehicles/{vehicle.id}")
        assert response.status_code == 200
        vehicle_data = response.json()
        assert vehicle_data["mileage"] == 35000

    @pytest.mark.asyncio
    async def test_get_mileage_history(self, owner_client: AsyncClient, test_customer: Customer, db: AsyncSession):
        """Get mileage history for a vehicle."""
        vehicle = VehicleFactory.build(customer_id=test_customer.id)
        db.add(vehicle)
        await db.commit()
        await db.refresh(vehicle)

        # Add two records
        for mileage in [20000, 25000]:
            response = await owner_client.post(
                f"/api/v1/vehicles/{vehicle.id}/mileage",
                json={"mileage": mileage, "source": "MANUAL"},
            )
            assert response.status_code == 201

        response = await owner_client.get(f"/api/v1/vehicles/{vehicle.id}/mileage")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

    @pytest.mark.asyncio
    async def test_add_mileage_invalid_value(self, owner_client: AsyncClient, test_customer: Customer, db: AsyncSession):
        """Mileage must be positive."""
        vehicle = VehicleFactory.build(customer_id=test_customer.id)
        db.add(vehicle)
        await db.commit()
        await db.refresh(vehicle)

        response = await owner_client.post(
            f"/api/v1/vehicles/{vehicle.id}/mileage",
            json={"mileage": 0, "source": "MANUAL"},
        )
        assert response.status_code == 422


class TestVehicleStatus:
    """Tests for vehicle status updates."""

    @pytest.mark.asyncio
    async def test_update_vehicle_status(self, owner_client: AsyncClient, test_customer: Customer, db: AsyncSession):
        """Update vehicle status to IN_SHOP."""
        vehicle = VehicleFactory.build(customer_id=test_customer.id)
        db.add(vehicle)
        await db.commit()
        await db.refresh(vehicle)

        response = await owner_client.patch(
            f"/api/v1/vehicles/{vehicle.id}/status",
            params={"status": "IN_SHOP"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "IN_SHOP"

    @pytest.mark.asyncio
    async def test_update_vehicle_status_invalid(self, owner_client: AsyncClient, test_customer: Customer, db: AsyncSession):
        """Invalid status should return 400."""
        vehicle = VehicleFactory.build(customer_id=test_customer.id)
        db.add(vehicle)
        await db.commit()
        await db.refresh(vehicle)

        response = await owner_client.patch(
            f"/api/v1/vehicles/{vehicle.id}/status",
            params={"status": "INVALID_STATUS"},
        )
        assert response.status_code == 400


class TestVehicleAuthorization:
    """RBAC tests for vehicle endpoints."""

    @pytest.mark.asyncio
    async def test_manager_can_create_vehicle(self, manager_client: AsyncClient, test_customer: Customer):
        """Service Advisor can create vehicles."""
        response = await manager_client.post(
            "/api/v1/vehicles/",
            json={"customer_id": str(test_customer.id), "make": "Honda", "model": "Civic"},
        )
        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_technician_can_read_vehicle(self, technician_client: AsyncClient, test_customer: Customer, db: AsyncSession):
        """Technician can read vehicles."""
        vehicle = VehicleFactory.build(customer_id=test_customer.id)
        db.add(vehicle)
        await db.commit()
        await db.refresh(vehicle)

        response = await technician_client.get(f"/api/v1/vehicles/{vehicle.id}")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_technician_cannot_create_vehicle(self, technician_client: AsyncClient, test_customer: Customer):
        """Technician cannot create vehicles."""
        response = await technician_client.post(
            "/api/v1/vehicles/",
            json={"customer_id": str(test_customer.id), "make": "Honda", "model": "Civic"},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_customer_cannot_read_the_shop_vehicle_list(
        self, customer_client: AsyncClient
    ):
        """The customer holds vehicles:read, and is still refused here.

        This endpoint is the shop's list of every vehicle on the premises, so a
        customer reaching it would be reading other people's cars. The permission
        is the customer's for the portal, which shows their own vehicles and
        nothing else; the shop's list is behind the staff gate.
        """
        response = await customer_client.get("/api/v1/vehicles/")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_unauthenticated_cannot_access_vehicles(self, unauth_client: AsyncClient):
        """Unauthenticated requests are rejected."""
        response = await unauth_client.get("/api/v1/vehicles/")
        assert response.status_code == 401
