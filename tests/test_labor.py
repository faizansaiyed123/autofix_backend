"""Tests for labor tracking and the technician dashboard.

Covers logging time against an in-progress repair order, the actual-vs-billable
split, the gate that blocks labor once an order finishes, task linkage, and the
technician dashboard aggregate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.customers.models import Customer
from app.vehicles.models import Vehicle
from tests.factories import VehicleFactory

LABOR_URL = "/api/v1/labor/"
REPAIR_ORDERS_URL = "/api/v1/repair_orders/"


@pytest.fixture()
async def test_customer(db: AsyncSession):
    customer = Customer(
        first_name="Labor",
        last_name="Customer",
        email="labor_test@example.com",
        phone="555-0020",
        preferred_contact="EMAIL",
        customer_status="ACTIVE",
    )
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    return customer


@pytest.fixture()
async def test_vehicle(db: AsyncSession, test_customer: Customer):
    vehicle = VehicleFactory.build(customer_id=test_customer.id)
    db.add(vehicle)
    await db.commit()
    await db.refresh(vehicle)
    return vehicle


async def get_demo_user_id(db: AsyncSession, email: str):
    """Resolve a seeded demo user's UUID by email."""
    from app.auth.services import AuthService

    user = await AuthService(db).get_user_by_email(email)
    assert user is not None, f"demo user {email} not seeded"
    return user.id


async def create_ro_in_progress(
    client: AsyncClient, customer: Customer, vehicle: Vehicle
) -> dict:
    """Create a repair order and drive it to IN_PROGRESS with one task."""
    create = await client.post(
        REPAIR_ORDERS_URL,
        json={
            "customer_id": str(customer.id),
            "vehicle_id": str(vehicle.id),
            "tasks": [{"description": "Replace pads"}],
        },
    )
    assert create.status_code == 201, create.text
    ro = create.json()
    for status_value in ("APPROVED", "IN_PROGRESS"):
        resp = await client.patch(
            f"{REPAIR_ORDERS_URL}{ro['id']}/status", params={"status": status_value}
        )
        assert resp.status_code == 200, resp.text
    return resp.json()


class TestLaborRecording:
    @pytest.mark.asyncio
    async def test_billable_defaults_to_actual(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """With no explicit billable value, billable equals actual hours."""
        ro = await create_ro_in_progress(owner_client, test_customer, test_vehicle)
        response = await owner_client.post(
            LABOR_URL,
            json={
                "repair_order_id": ro["id"],
                "description": "Brake work",
                "actual_hours": 2.5,
                "hourly_rate": 100,
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["actual_hours"] == 2.5
        assert body["billable_hours"] == 2.5
        assert body["labor_cost"] == 250.0

    @pytest.mark.asyncio
    async def test_explicit_billable_differs_from_actual(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Billable hours may be set independently of actual hours."""
        ro = await create_ro_in_progress(owner_client, test_customer, test_vehicle)
        response = await owner_client.post(
            LABOR_URL,
            json={
                "repair_order_id": ro["id"],
                "description": "Diagnostic",
                "actual_hours": 3,
                "billable_hours": 1,
                "hourly_rate": 80,
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["billable_hours"] == 1.0
        assert body["labor_cost"] == 80.0

    @pytest.mark.asyncio
    async def test_labor_linked_to_task(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Labor may be recorded against a specific task of the order."""
        ro = await create_ro_in_progress(owner_client, test_customer, test_vehicle)
        task_id = ro["tasks"][0]["id"]
        response = await owner_client.post(
            LABOR_URL,
            json={
                "repair_order_id": ro["id"],
                "repair_task_id": task_id,
                "description": "On-task work",
                "actual_hours": 1,
            },
        )
        assert response.status_code == 201
        assert response.json()["repair_task_id"] == task_id

    @pytest.mark.asyncio
    async def test_task_from_other_order_rejected(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A task belonging to a different order cannot be linked."""
        ro1 = await create_ro_in_progress(owner_client, test_customer, test_vehicle)
        ro2 = await create_ro_in_progress(owner_client, test_customer, test_vehicle)
        response = await owner_client.post(
            LABOR_URL,
            json={
                "repair_order_id": ro1["id"],
                "repair_task_id": ro2["tasks"][0]["id"],
                "description": "Mismatch",
                "actual_hours": 1,
            },
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_cannot_log_before_ro_starts(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Labor is blocked while the order has not started (DRAFT)."""
        create = await owner_client.post(
            REPAIR_ORDERS_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "tasks": [{"description": "Task"}],
            },
        )
        ro = create.json()
        response = await owner_client.post(
            LABOR_URL,
            json={
                "repair_order_id": ro["id"],
                "description": "Too early",
                "actual_hours": 1,
            },
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_cannot_log_after_ro_completed(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Labor is blocked once the order has been completed."""
        ro = await create_ro_in_progress(owner_client, test_customer, test_vehicle)
        await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro['id']}/tasks/{ro['tasks'][0]['id']}/status",
            params={"status": "COMPLETED"},
        )
        completed = await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro['id']}/status", params={"status": "COMPLETED"}
        )
        assert completed.status_code == 200
        response = await owner_client.post(
            LABOR_URL,
            json={
                "repair_order_id": ro["id"],
                "description": "Too late",
                "actual_hours": 1,
            },
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_unknown_technician_rejected(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """An unknown technician reference is a conflict."""
        ro = await create_ro_in_progress(owner_client, test_customer, test_vehicle)
        response = await owner_client.post(
            LABOR_URL,
            json={
                "repair_order_id": ro["id"],
                "technician_id": str(uuid4()),
                "description": "Ghost tech",
                "actual_hours": 1,
            },
        )
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_invalid_hours_rejected(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Non-positive actual hours are rejected by validation."""
        ro = await create_ro_in_progress(owner_client, test_customer, test_vehicle)
        response = await owner_client.post(
            LABOR_URL,
            json={
                "repair_order_id": ro["id"],
                "description": "Zero hours",
                "actual_hours": 0,
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_update_and_delete_labor(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Labor records can be edited and removed while work is ongoing."""
        ro = await create_ro_in_progress(owner_client, test_customer, test_vehicle)
        created = await owner_client.post(
            LABOR_URL,
            json={
                "repair_order_id": ro["id"],
                "description": "Work",
                "actual_hours": 1,
            },
        )
        labor_id = created.json()["id"]

        updated = await owner_client.patch(
            f"{LABOR_URL}{labor_id}", json={"actual_hours": 4}
        )
        assert updated.status_code == 200
        assert updated.json()["actual_hours"] == 4.0

        deleted = await owner_client.delete(f"{LABOR_URL}{labor_id}")
        assert deleted.status_code == 204

    @pytest.mark.asyncio
    async def test_list_labor(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """List labor records filtered by repair order."""
        ro = await create_ro_in_progress(owner_client, test_customer, test_vehicle)
        await owner_client.post(
            LABOR_URL,
            json={
                "repair_order_id": ro["id"],
                "description": "Work",
                "actual_hours": 1,
            },
        )
        response = await owner_client.get(
            f"{LABOR_URL}?repair_order_id={ro['id']}"
        )
        assert response.status_code == 200
        assert response.json()["meta"]["total"] == 1


class TestTechnicianDashboard:
    @pytest.mark.asyncio
    async def test_dashboard_counts(
        self,
        owner_client: AsyncClient,
        db: AsyncSession,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """Dashboard surfaces open tasks and hours logged this week."""
        tech_id = await get_demo_user_id(db, "tech@autofix.demo")
        ro = await create_ro_in_progress(owner_client, test_customer, test_vehicle)
        # Assign the RO's task to the demo technician.
        assign = await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro['id']}/tasks/{ro['tasks'][0]['id']}",
            json={"assigned_to_id": str(tech_id)},
        )
        assert assign.status_code == 200

        await owner_client.post(
            LABOR_URL,
            json={
                "repair_order_id": ro["id"],
                "technician_id": str(tech_id),
                "description": "Recent work",
                "actual_hours": 2,
                "performed_at": datetime.now(UTC).isoformat(),
            },
        )

        response = await owner_client.get(f"{LABOR_URL}dashboard?technician_id={tech_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["technician_id"] == str(tech_id)
        assert body["open_task_count"] == 1
        assert body["hours_this_week"] == 2.0
        assert body["open_tasks"][0]["ro_number"] == ro["ro_number"]


class TestLaborRbac:
    @pytest.mark.asyncio
    async def test_technician_can_log_labor(
        self,
        technician_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """Technicians hold labor:write."""
        ro = await create_ro_in_progress(technician_client, test_customer, test_vehicle)
        response = await technician_client.post(
            LABOR_URL,
            json={
                "repair_order_id": ro["id"],
                "description": "Tech work",
                "actual_hours": 1,
            },
        )
        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_customer_cannot_log_labor(
        self,
        customer_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """Customers hold no labor permissions."""
        response = await customer_client.get(LABOR_URL)
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_parts_staff_cannot_log_labor(
        self, parts_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Parts staff hold no labor permissions."""
        response = await parts_client.get(LABOR_URL)
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_unauthenticated_cannot_read(self, unauth_client: AsyncClient):
        """Anonymous callers are rejected."""
        response = await unauth_client.get(LABOR_URL)
        assert response.status_code == 401
