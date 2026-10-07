"""Tests for inspection management endpoints.

Tests cover CRUD operations, status transitions, items, report generation,
and RBAC authorization.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.checkins.models import CheckIn
from app.customers.models import Customer
from app.vehicles.models import Vehicle
from tests.factories import VehicleFactory


@pytest.fixture()
async def test_customer(db: AsyncSession):
    """Create a test customer."""
    customer = Customer(
        first_name="Inspect",
        last_name="Customer",
        email="inspect_test@example.com",
        phone="555-0003",
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


@pytest.fixture()
async def test_checkin(db: AsyncSession, test_customer: Customer, test_vehicle: Vehicle):
    """Create a test check-in."""
    checkin = CheckIn(
        customer_id=test_customer.id,
        vehicle_id=test_vehicle.id,
        odometer=35000,
        checkin_type="APPOINTMENT",
    )
    db.add(checkin)
    await db.commit()
    await db.refresh(checkin)
    return checkin


class TestInspectionCRUD:
    """Tests for inspection CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_inspection_owner(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Owner can create an inspection."""
        response = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
                "overall_notes": "Full inspection",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["vehicle_id"] == str(test_vehicle.id)
        assert data["customer_id"] == str(test_customer.id)
        assert data["status"] == "DRAFT"
        assert data["overall_notes"] == "Full inspection"
        assert data["items"] == []

    @pytest.mark.asyncio
    async def test_create_inspection_with_items(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Owner can create an inspection with items."""
        response = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
                "overall_notes": "Detailed inspection",
                "items": [
                    {
                        "category": "brakes",
                        "item_name": "Front Brake Pads",
                        "status": "GOOD",
                        "measurement": "4mm",
                        "recommendation": "PASS",
                    },
                    {
                        "category": "engine",
                        "item_name": "Oil Level",
                        "status": "ATTENTION",
                        "measurement": "Low",
                        "notes": "Top up needed",
                    },
                ],
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert len(data["items"]) == 2
        assert data["items"][0]["item_name"] == "Front Brake Pads"
        assert data["items"][1]["status"] == "ATTENTION"

    @pytest.mark.asyncio
    async def test_create_inspection_invalid_customer(self, owner_client: AsyncClient, test_vehicle: Vehicle):
        """Inspection with non-existent customer should fail."""
        response = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(uuid4()),
            },
        )
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_create_inspection_invalid_vehicle(self, owner_client: AsyncClient, test_customer: Customer):
        """Inspection with non-existent vehicle should fail."""
        response = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(uuid4()),
                "customer_id": str(test_customer.id),
            },
        )
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_get_inspection_owner(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Owner can get an inspection by ID."""
        create_resp = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
            },
        )
        inspection_id = create_resp.json()["id"]

        response = await owner_client.get(f"/api/v1/inspections/{inspection_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == inspection_id

    @pytest.mark.asyncio
    async def test_get_inspection_not_found(self, owner_client: AsyncClient):
        """Non-existent inspection returns 404."""
        response = await owner_client.get(f"/api/v1/inspections/{uuid4()}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_update_inspection(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Owner can update inspection notes."""
        create_resp = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
            },
        )
        inspection_id = create_resp.json()["id"]

        response = await owner_client.patch(
            f"/api/v1/inspections/{inspection_id}",
            json={"overall_notes": "Updated notes", "status": "IN_PROGRESS"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["overall_notes"] == "Updated notes"
        assert data["status"] == "IN_PROGRESS"

    @pytest.mark.asyncio
    async def test_list_inspections(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Owner can list inspections."""
        await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
            },
        )
        response = await owner_client.get("/api/v1/inspections/")
        assert response.status_code == 200
        data = response.json()
        assert data["meta"]["total"] >= 1
        assert len(data["data"]) >= 1


class TestInspectionStatus:
    """Tests for inspection status transitions."""

    @pytest.mark.asyncio
    async def test_status_transition_draft_to_in_progress(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Transition from DRAFT to IN_PROGRESS is allowed."""
        create_resp = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
            },
        )
        inspection_id = create_resp.json()["id"]

        response = await owner_client.patch(
            f"/api/v1/inspections/{inspection_id}/status",
            params={"status": "IN_PROGRESS"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "IN_PROGRESS"

    @pytest.mark.asyncio
    async def test_status_transition_in_progress_to_completed(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Transition from IN_PROGRESS to COMPLETED is allowed once items exist."""
        create_resp = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
                "items": [
                    {"category": "brakes", "item_name": "Brake Pads", "status": "GOOD"},
                ],
            },
        )
        inspection_id = create_resp.json()["id"]

        await owner_client.patch(
            f"/api/v1/inspections/{inspection_id}/status",
            params={"status": "IN_PROGRESS"},
        )

        response = await owner_client.patch(
            f"/api/v1/inspections/{inspection_id}/status",
            params={"status": "COMPLETED"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "COMPLETED"

    @pytest.mark.asyncio
    async def test_status_transition_draft_to_completed_invalid(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Transition from DRAFT to COMPLETED is not allowed."""
        create_resp = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
            },
        )
        inspection_id = create_resp.json()["id"]

        response = await owner_client.patch(
            f"/api/v1/inspections/{inspection_id}/status",
            params={"status": "COMPLETED"},
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_status_transition_invalid_status(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Invalid status value returns 400."""
        create_resp = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
            },
        )
        inspection_id = create_resp.json()["id"]

        response = await owner_client.patch(
            f"/api/v1/inspections/{inspection_id}/status",
            params={"status": "INVALID_STATUS"},
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_status_transition_draft_to_cancelled(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Transition from DRAFT to CANCELLED is allowed."""
        create_resp = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
            },
        )
        inspection_id = create_resp.json()["id"]

        response = await owner_client.patch(
            f"/api/v1/inspections/{inspection_id}/status",
            params={"status": "CANCELLED"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "CANCELLED"


class TestInspectionItems:
    """Tests for inspection item management."""

    @pytest.mark.asyncio
    async def test_add_item(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Owner can add an item to an existing inspection."""
        create_resp = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
            },
        )
        inspection_id = create_resp.json()["id"]

        response = await owner_client.post(
            f"/api/v1/inspections/{inspection_id}/items",
            json={
                "category": "tires",
                "item_name": "Tire Pressure",
                "status": "URGENT",
                "measurement": "28 PSI",
                "recommendation": "ADJUST",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["item_name"] == "Tire Pressure"
        assert data["items"][0]["status"] == "URGENT"

    @pytest.mark.asyncio
    async def test_add_item_with_photo(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Owner can add an item with a photo."""
        create_resp = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
            },
        )
        inspection_id = create_resp.json()["id"]

        response = await owner_client.post(
            f"/api/v1/inspections/{inspection_id}/items",
            json={
                "category": "brakes",
                "item_name": "Rear Brake Pads",
                "status": "ATTENTION",
                "measurement": "3mm",
                "recommendation": "INSPECT_FURTHER",
                "photo_url": "https://example.com/photo1.jpg",
                "photo_caption": "Rear brake wear",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["photo_url"] == "https://example.com/photo1.jpg"
        assert len(data["items"][0]["photos"]) == 1


class TestInspectionReport:
    """Tests for report generation."""

    @pytest.mark.asyncio
    async def test_generate_report(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Owner can generate an inspection report."""
        create_resp = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
                "overall_notes": "Routine inspection",
                "items": [
                    {
                        "category": "brakes",
                        "item_name": "Brake Pads",
                        "status": "GOOD",
                    },
                    {
                        "category": "engine",
                        "item_name": "Oil Level",
                        "status": "ATTENTION",
                    },
                    {
                        "category": "tires",
                        "item_name": "Tire Condition",
                        "status": "URGENT",
                    },
                ],
            },
        )
        inspection_id = create_resp.json()["id"]

        response = await owner_client.get(f"/api/v1/inspections/{inspection_id}/report")
        assert response.status_code == 200
        data = response.json()
        assert data["inspection_id"] == inspection_id
        assert data["summary"]["total_items"] == 3
        assert data["summary"]["green"] == 1
        assert data["summary"]["yellow"] == 1
        assert data["summary"]["red"] == 1

        # Worst-first ordering puts the RED category at the top.
        categories = {c["category"]: c for c in data["categories"]}
        assert set(categories) == {"brakes", "engine", "tires"}
        assert data["categories"][0]["category"] == "tires"
        assert categories["tires"]["severity_color"] == "RED"
        assert categories["engine"]["severity_color"] == "YELLOW"
        assert categories["brakes"]["severity_color"] == "GREEN"

        assert data["overall_condition"] == "RED"
        assert [i["item_name"] for i in data["urgent_items"]] == ["Tire Condition"]
        assert [i["item_name"] for i in data["recommended_items"]] == ["Oil Level"]


class TestInspectionAuthorization:
    """RBAC tests for inspection endpoints."""

    @pytest.mark.asyncio
    async def test_manager_can_create_inspection(self, manager_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Service Advisor can create inspections."""
        response = await manager_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
            },
        )
        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_technician_can_read_inspections(self, technician_client: AsyncClient):
        """Technician can read inspections."""
        response = await technician_client.get("/api/v1/inspections/")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_technician_can_write_inspections(self, technician_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Technician can write inspections."""
        response = await technician_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
            },
        )
        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_customer_cannot_read_the_shop_inspection_list(
        self, customer_client: AsyncClient
    ):
        """The customer holds inspections:read, and is still refused here.

        The shop's list is every inspection the business has ever recorded, on
        every customer's cars. The customer's own inspections are on their vehicle
        history in the portal.
        """
        response = await customer_client.get("/api/v1/inspections/")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_customer_cannot_create_inspection(self, customer_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Customer cannot create inspections (read-only)."""
        response = await customer_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
            },
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_parts_staff_cannot_access(self, parts_client: AsyncClient):
        """Parts staff cannot access inspections."""
        response = await parts_client.get("/api/v1/inspections/")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_unauthenticated_cannot_access(self, unauth_client: AsyncClient):
        """Unauthenticated requests are rejected."""
        response = await unauth_client.get("/api/v1/inspections/")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_delete_inspection_owner(self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle):
        """Owner can delete an inspection."""
        create_resp = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
            },
        )
        inspection_id = create_resp.json()["id"]

        response = await owner_client.delete(f"/api/v1/inspections/{inspection_id}")
        assert response.status_code == 204

        get_resp = await owner_client.get(f"/api/v1/inspections/{inspection_id}")
        assert get_resp.status_code == 404


class TestInspectionItemValidation:
    """Validation of inspection item status and recommendation vocabularies."""

    async def _create_inspection(self, client: AsyncClient, customer: Customer, vehicle: Vehicle) -> str:
        resp = await client.post(
            "/api/v1/inspections/",
            json={"vehicle_id": str(vehicle.id), "customer_id": str(customer.id)},
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    @pytest.mark.asyncio
    async def test_invalid_item_status_rejected(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """An item status outside the allowed vocabulary is rejected."""
        response = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
                "items": [
                    {"category": "brakes", "item_name": "Pads", "status": "TOTALLY_FINE"}
                ],
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_recommendation_rejected(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A recommendation outside the allowed vocabulary is rejected."""
        response = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
                "items": [
                    {
                        "category": "brakes",
                        "item_name": "Pads",
                        "status": "GOOD",
                        "recommendation": "SET_ON_FIRE",
                    }
                ],
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_item_status_is_case_insensitive(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Lowercase status values are normalised to the canonical form."""
        response = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
                "items": [{"category": "tires", "item_name": "Tread", "status": "urgent"}],
            },
        )
        assert response.status_code == 201
        assert response.json()["items"][0]["status"] == "URGENT"

    @pytest.mark.asyncio
    async def test_item_carries_severity_color(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Each item exposes its traffic-light colour."""
        response = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
                "items": [
                    {"category": "tires", "item_name": "Tread", "status": "RECOMMENDED"},
                ],
            },
        )
        assert response.json()["items"][0]["severity_color"] == "YELLOW"


class TestInspectionBusinessRules:
    """Business rules guarding inspection lifecycle and relationships."""

    @pytest.mark.asyncio
    async def test_cannot_complete_inspection_without_items(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """An inspection with no findings cannot be marked COMPLETED."""
        create_resp = await owner_client.post(
            "/api/v1/inspections/",
            json={"vehicle_id": str(test_vehicle.id), "customer_id": str(test_customer.id)},
        )
        inspection_id = create_resp.json()["id"]

        await owner_client.patch(
            f"/api/v1/inspections/{inspection_id}/status", params={"status": "IN_PROGRESS"}
        )
        response = await owner_client.patch(
            f"/api/v1/inspections/{inspection_id}/status", params={"status": "COMPLETED"}
        )
        assert response.status_code == 400
        assert "no inspection items" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_vehicle_must_belong_to_customer(
        self, owner_client: AsyncClient, db: AsyncSession, test_vehicle: Vehicle
    ):
        """An inspection cannot pair a vehicle with someone else's customer record."""
        other = Customer(
            first_name="Other",
            last_name="Owner",
            email="other_owner@example.com",
            preferred_contact="EMAIL",
            customer_status="ACTIVE",
        )
        db.add(other)
        await db.commit()
        await db.refresh(other)

        response = await owner_client.post(
            "/api/v1/inspections/",
            json={"vehicle_id": str(test_vehicle.id), "customer_id": str(other.id)},
        )
        assert response.status_code == 400
        assert "does not belong to customer" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_cannot_add_item_to_completed_inspection(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A completed inspection is frozen against further findings."""
        create_resp = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
                "items": [{"category": "brakes", "item_name": "Pads", "status": "GOOD"}],
            },
        )
        inspection_id = create_resp.json()["id"]

        await owner_client.patch(
            f"/api/v1/inspections/{inspection_id}/status", params={"status": "IN_PROGRESS"}
        )
        await owner_client.patch(
            f"/api/v1/inspections/{inspection_id}/status", params={"status": "COMPLETED"}
        )

        response = await owner_client.post(
            f"/api/v1/inspections/{inspection_id}/items",
            json={"category": "tires", "item_name": "Tread", "status": "GOOD"},
        )
        assert response.status_code == 400


class TestInspectionItemEditing:
    """Updating and removing individual inspection items."""

    @pytest.mark.asyncio
    async def test_update_item(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A technician can revise an item's finding."""
        create_resp = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
                "items": [{"category": "brakes", "item_name": "Pads", "status": "GOOD"}],
            },
        )
        inspection_id = create_resp.json()["id"]
        item_id = create_resp.json()["items"][0]["id"]

        response = await owner_client.patch(
            f"/api/v1/inspections/{inspection_id}/items/{item_id}",
            json={"status": "URGENT", "measurement": "1mm", "notes": "Metal on metal"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "URGENT"
        assert data["severity_color"] == "RED"
        assert data["measurement"] == "1mm"

    @pytest.mark.asyncio
    async def test_delete_item(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """An item can be removed from an open inspection."""
        create_resp = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
                "items": [{"category": "brakes", "item_name": "Pads", "status": "GOOD"}],
            },
        )
        inspection_id = create_resp.json()["id"]
        item_id = create_resp.json()["items"][0]["id"]

        response = await owner_client.delete(
            f"/api/v1/inspections/{inspection_id}/items/{item_id}"
        )
        assert response.status_code == 204

        get_resp = await owner_client.get(f"/api/v1/inspections/{inspection_id}")
        assert get_resp.json()["items"] == []

    @pytest.mark.asyncio
    async def test_update_unknown_item_returns_404(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Updating an item that isn't on the inspection returns 404."""
        create_resp = await owner_client.post(
            "/api/v1/inspections/",
            json={"vehicle_id": str(test_vehicle.id), "customer_id": str(test_customer.id)},
        )
        inspection_id = create_resp.json()["id"]

        response = await owner_client.patch(
            f"/api/v1/inspections/{inspection_id}/items/{uuid4()}",
            json={"status": "GOOD"},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_item_supports_multiple_photos(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """An item can carry several photos; photo_url exposes the first."""
        response = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
                "items": [
                    {
                        "category": "brakes",
                        "item_name": "Rotors",
                        "status": "URGENT",
                        "photos": [
                            {"photo_url": "https://example.com/a.jpg", "caption": "Inner"},
                            {"photo_url": "https://example.com/b.jpg", "caption": "Outer"},
                        ],
                    }
                ],
            },
        )
        assert response.status_code == 201
        item = response.json()["items"][0]
        assert len(item["photos"]) == 2
        assert item["photo_url"] == "https://example.com/a.jpg"

    @pytest.mark.asyncio
    async def test_deleting_inspection_removes_items_and_photos(
        self,
        owner_client: AsyncClient,
        db: AsyncSession,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """Cascade delete cleans up items and photos, leaving no orphans."""
        from sqlalchemy import func, select

        from app.inspections.models import InspectionItem, InspectionPhoto

        create_resp = await owner_client.post(
            "/api/v1/inspections/",
            json={
                "vehicle_id": str(test_vehicle.id),
                "customer_id": str(test_customer.id),
                "items": [
                    {
                        "category": "brakes",
                        "item_name": "Rotors",
                        "status": "URGENT",
                        "photo_url": "https://example.com/a.jpg",
                    }
                ],
            },
        )
        inspection_id = create_resp.json()["id"]

        assert (await owner_client.delete(f"/api/v1/inspections/{inspection_id}")).status_code == 204

        items = await db.execute(
            select(func.count()).select_from(InspectionItem).where(
                InspectionItem.inspection_id == inspection_id
            )
        )
        photos = await db.execute(
            select(func.count()).select_from(InspectionPhoto).where(
                InspectionPhoto.inspection_id == inspection_id
            )
        )
        assert items.scalar_one() == 0
        assert photos.scalar_one() == 0
