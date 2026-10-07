"""Tests for repair orders and task breakdowns.

Covers repair-order CRUD, creation from an approved estimate (including task
seeding), the RO and task status machines, task-freezing once work starts,
completion guards, deletion rules, summaries, and RBAC.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.customers.models import Customer
from app.vehicles.models import Vehicle
from tests.factories import VehicleFactory

REPAIR_ORDERS_URL = "/api/v1/repair_orders/"
ESTIMATES_URL = "/api/v1/estimates/"


@pytest.fixture()
async def test_customer(db: AsyncSession):
    """Create a customer to hang repair orders off."""
    customer = Customer(
        first_name="Repair",
        last_name="Customer",
        email="repair_test@example.com",
        phone="555-0010",
        preferred_contact="EMAIL",
        customer_status="ACTIVE",
    )
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    return customer


@pytest.fixture()
async def test_vehicle(db: AsyncSession, test_customer: Customer):
    """Create a vehicle owned by the test customer."""
    vehicle = VehicleFactory.build(customer_id=test_customer.id)
    db.add(vehicle)
    await db.commit()
    await db.refresh(vehicle)
    return vehicle


@pytest.fixture()
async def other_customer(db: AsyncSession):
    """A second customer, used for ownership-mismatch cases."""
    customer = Customer(
        first_name="Other",
        last_name="Repair",
        email="repair_other@example.com",
        phone="555-0011",
        preferred_contact="EMAIL",
        customer_status="ACTIVE",
    )
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    return customer


@pytest.fixture()
async def other_vehicle(db: AsyncSession, other_customer: Customer):
    """A vehicle belonging to a different customer."""
    vehicle = VehicleFactory.build(customer_id=other_customer.id)
    db.add(vehicle)
    await db.commit()
    await db.refresh(vehicle)
    return vehicle


async def create_repair_order(
    client: AsyncClient,
    customer: Customer,
    vehicle: Vehicle,
    tasks: list[dict] | None = None,
    **overrides,
) -> dict:
    """Create a repair order through the API and return its JSON body."""
    payload = {
        "customer_id": str(customer.id),
        "vehicle_id": str(vehicle.id),
    }
    if tasks is not None:
        payload["tasks"] = tasks
    payload.update(overrides)
    response = await client.post(REPAIR_ORDERS_URL, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def create_approved_estimate(
    client: AsyncClient, customer: Customer, vehicle: Vehicle
) -> dict:
    """Create a fully-approved estimate with two billable lines."""
    create = await client.post(
        ESTIMATES_URL,
        json={
            "customer_id": str(customer.id),
            "vehicle_id": str(vehicle.id),
            "items": [
                {
                    "item_type": "LABOR",
                    "description": "Brake pad replacement",
                    "labor_hours": 2,
                    "labor_rate": 95,
                },
                {
                    "item_type": "PART",
                    "description": "Ceramic pad set",
                    "part_number": "BP-1",
                    "quantity": 1,
                    "unit_price": 45,
                },
            ],
        },
    )
    assert create.status_code == 201, create.text
    estimate = create.json()

    send = await client.post(f"{ESTIMATES_URL}{estimate['id']}/send")
    assert send.status_code == 200, send.text

    # Approve every line so the estimate reaches APPROVED.
    current = send.json()
    for item in current["items"]:
        decision = await client.post(
            f"{ESTIMATES_URL}{current['id']}/items/{item['id']}/decision",
            json={"decision": "APPROVED"},
        )
        assert decision.status_code == 200, decision.text
    return decision.json()


class TestRepairOrderCrud:
    @pytest.mark.asyncio
    async def test_create_ro_with_tasks(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Create a repair order carrying an explicit task breakdown."""
        created = await create_repair_order(
            owner_client,
            test_customer,
            test_vehicle,
            tasks=[
                {"description": "Replace brake pads"},
                {"description": "Bleed brakes"},
            ],
        )
        assert created["status"] == "DRAFT"
        assert len(created["tasks"]) == 2
        assert created["tasks"][0]["description"] == "Replace brake pads"
        assert created["tasks"][0]["status"] == "PENDING"

    @pytest.mark.asyncio
    async def test_ro_number_is_unique(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Each RO gets its own RO number."""
        first = await create_repair_order(owner_client, test_customer, test_vehicle)
        second = await create_repair_order(owner_client, test_customer, test_vehicle)
        assert first["ro_number"] != second["ro_number"]
        assert first["ro_number"].startswith("RO-")

    @pytest.mark.asyncio
    async def test_get_ro(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Fetch a single RO by ID."""
        created = await create_repair_order(owner_client, test_customer, test_vehicle)
        response = await owner_client.get(f"{REPAIR_ORDERS_URL}{created['id']}")
        assert response.status_code == 200
        assert response.json()["id"] == created["id"]

    @pytest.mark.asyncio
    async def test_list_ros(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """List repair orders for the customer."""
        await create_repair_order(owner_client, test_customer, test_vehicle)
        response = await owner_client.get(
            f"{REPAIR_ORDERS_URL}?customer_id={test_customer.id}"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["meta"]["total"] == 1

    @pytest.mark.asyncio
    async def test_update_ro_header(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Update mutable RO header fields."""
        created = await create_repair_order(owner_client, test_customer, test_vehicle)
        response = await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{created['id']}",
            json={"bay": "BAY-1", "odometer_in": 100000, "notes": "rattle on cold start"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["bay"] == "BAY-1"
        assert body["odometer_in"] == 100000

    @pytest.mark.asyncio
    async def test_update_ro_rejects_bad_odometer(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Negative odometer readings are rejected by validation."""
        created = await create_repair_order(owner_client, test_customer, test_vehicle)
        response = await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{created['id']}", json={"odometer_in": -5}
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_rejects_mismatched_vehicle(
        self,
        owner_client: AsyncClient,
        test_customer: Customer,
        other_vehicle: Vehicle,
    ):
        """A vehicle owned by another customer cannot be attached."""
        response = await owner_client.post(
            REPAIR_ORDERS_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(other_vehicle.id),
            },
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_create_rejects_unknown_customer(
        self, owner_client: AsyncClient, test_vehicle: Vehicle
    ):
        """An unknown customer is a conflict."""
        response = await owner_client.post(
            REPAIR_ORDERS_URL,
            json={
                "customer_id": str(uuid4()),
                "vehicle_id": str(test_vehicle.id),
            },
        )
        assert response.status_code == 409


class TestRepairOrderFromEstimate:
    @pytest.mark.asyncio
    async def test_create_from_approved_estimate_seeds_tasks(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """An RO built from an approved estimate inherits its task breakdown."""
        estimate = await create_approved_estimate(
            owner_client, test_customer, test_vehicle
        )
        assert estimate["status"] == "APPROVED"

        created = await create_repair_order(
            owner_client, test_customer, test_vehicle, estimate_id=estimate["id"]
        )
        assert created["estimate_id"] == estimate["id"]
        # One task per approved, billable line.
        descriptions = [t["description"] for t in created["tasks"]]
        assert descriptions == ["Brake pad replacement", "Ceramic pad set"]

    @pytest.mark.asyncio
    async def test_explicit_tasks_override_estimate_seeding(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Providing tasks explicitly skips estimate seeding."""
        estimate = await create_approved_estimate(
            owner_client, test_customer, test_vehicle
        )
        created = await create_repair_order(
            owner_client,
            test_customer,
            test_vehicle,
            tasks=[{"description": "Custom task"}],
            estimate_id=estimate["id"],
        )
        assert len(created["tasks"]) == 1
        assert created["tasks"][0]["description"] == "Custom task"

    @pytest.mark.asyncio
    async def test_create_rejects_draft_estimate(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A RO can only be raised against an approved estimate."""
        create = await owner_client.post(
            ESTIMATES_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "items": [
                    {
                        "item_type": "PART",
                        "description": "Some part",
                        "unit_price": 20,
                    }
                ],
            },
        )
        draft = create.json()

        response = await owner_client.post(
            REPAIR_ORDERS_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "estimate_id": draft["id"],
            },
        )
        assert response.status_code == 400


class TestRepairOrderStatusMachine:
    @pytest.mark.asyncio
    async def test_approve_draft(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A DRAFT RO can be approved."""
        created = await create_repair_order(owner_client, test_customer, test_vehicle)
        response = await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{created['id']}/status", params={"status": "APPROVED"}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "APPROVED"

    @pytest.mark.asyncio
    async def test_start_requires_tasks(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Work cannot start on an RO with no task breakdown."""
        created = await create_repair_order(owner_client, test_customer, test_vehicle)
        await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{created['id']}/status", params={"status": "APPROVED"}
        )
        response = await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{created['id']}/status", params={"status": "IN_PROGRESS"}
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_start_sets_started_at(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Starting work stamps started_at."""
        created = await create_repair_order(
            owner_client,
            test_customer,
            test_vehicle,
            tasks=[{"description": "Replace pads"}],
        )
        await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{created['id']}/status", params={"status": "APPROVED"}
        )
        response = await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{created['id']}/status", params={"status": "IN_PROGRESS"}
        )
        assert response.status_code == 200
        assert response.json()["started_at"] is not None

    @pytest.mark.asyncio
    async def test_cannot_skip_from_draft_to_in_progress(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """DRAFT cannot jump straight to IN_PROGRESS."""
        created = await create_repair_order(
            owner_client,
            test_customer,
            test_vehicle,
            tasks=[{"description": "Replace pads"}],
        )
        response = await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{created['id']}/status",
            params={"status": "IN_PROGRESS"},
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_status_rejected(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """An unknown status is a business-rule error."""
        created = await create_repair_order(owner_client, test_customer, test_vehicle)
        response = await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{created['id']}/status",
            params={"status": "NOT_A_STATUS"},
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_complete_requires_all_tasks_done(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """An RO cannot complete while any task is still open."""
        created = await create_repair_order(
            owner_client,
            test_customer,
            test_vehicle,
            tasks=[{"description": "Task A"}, {"description": "Task B"}],
        )
        await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{created['id']}/status", params={"status": "APPROVED"}
        )
        await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{created['id']}/status", params={"status": "IN_PROGRESS"}
        )
        response = await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{created['id']}/status", params={"status": "COMPLETED"}
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_complete_after_all_tasks_done(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Once every task is done the RO can complete."""
        created = await create_repair_order(
            owner_client,
            test_customer,
            test_vehicle,
            tasks=[{"description": "Task A"}, {"description": "Task B"}],
        )
        ro_id = created["id"]
        await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro_id}/status", params={"status": "APPROVED"}
        )
        await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro_id}/status", params={"status": "IN_PROGRESS"}
        )
        for task in created["tasks"]:
            done = await owner_client.patch(
                f"{REPAIR_ORDERS_URL}{ro_id}/tasks/{task['id']}/status",
                params={"status": "COMPLETED"},
            )
            assert done.status_code == 200

        response = await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro_id}/status", params={"status": "COMPLETED"}
        )
        assert response.status_code == 200
        assert response.json()["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_full_lifecycle_to_delivered(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Drive an RO all the way from draft to delivered."""
        created = await create_repair_order(
            owner_client,
            test_customer,
            test_vehicle,
            tasks=[{"description": "Replace pads"}],
        )
        ro_id = created["id"]
        for status_value in (
            "APPROVED",
            "IN_PROGRESS",
        ):
            response = await owner_client.patch(
                f"{REPAIR_ORDERS_URL}{ro_id}/status", params={"status": status_value}
            )
            assert response.status_code == 200, response.text
        task_id = created["tasks"][0]["id"]
        await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro_id}/tasks/{task_id}/status",
            params={"status": "COMPLETED"},
        )
        for status_value in ("COMPLETED", "QC_PASSED", "DELIVERED"):
            response = await owner_client.patch(
                f"{REPAIR_ORDERS_URL}{ro_id}/status", params={"status": status_value}
            )
            assert response.status_code == 200, response.text
        final = response.json()
        assert final["status"] == "DELIVERED"
        assert final["delivered_at"] is not None

    @pytest.mark.asyncio
    async def test_terminal_status_is_stuck(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A cancelled RO accepts no further transitions."""
        created = await create_repair_order(owner_client, test_customer, test_vehicle)
        cancel = await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{created['id']}/status", params={"status": "CANCELLED"}
        )
        assert cancel.status_code == 200
        reopen = await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{created['id']}/status", params={"status": "APPROVED"}
        )
        assert reopen.status_code == 400

    @pytest.mark.asyncio
    async def test_cancel_records_reason(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Cancelling stores the supplied reason."""
        created = await create_repair_order(owner_client, test_customer, test_vehicle)
        response = await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{created['id']}/status",
            params={"status": "CANCELLED", "reason": "customer sold car"},
        )
        assert response.status_code == 200
        assert response.json()["cancel_reason"] == "customer sold car"


class TestRepairTasks:
    @pytest.mark.asyncio
    async def test_add_task(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Append a task to a draft RO."""
        created = await create_repair_order(owner_client, test_customer, test_vehicle)
        response = await owner_client.post(
            f"{REPAIR_ORDERS_URL}{created['id']}/tasks",
            json={"description": "New task"},
        )
        assert response.status_code == 201
        assert len(response.json()["tasks"]) == 1

    @pytest.mark.asyncio
    async def test_update_task(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Rename a task."""
        created = await create_repair_order(
            owner_client,
            test_customer,
            test_vehicle,
            tasks=[{"description": "Old name"}],
        )
        task_id = created["tasks"][0]["id"]
        response = await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{created['id']}/tasks/{task_id}",
            json={"description": "New name"},
        )
        assert response.status_code == 200
        assert response.json()["tasks"][0]["description"] == "New name"

    @pytest.mark.asyncio
    async def test_delete_task(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Remove a task from a draft RO."""
        created = await create_repair_order(
            owner_client,
            test_customer,
            test_vehicle,
            tasks=[{"description": "Task to remove"}],
        )
        task_id = created["tasks"][0]["id"]
        response = await owner_client.delete(
            f"{REPAIR_ORDERS_URL}{created['id']}/tasks/{task_id}"
        )
        assert response.status_code == 200
        assert response.json()["tasks"] == []

    @pytest.mark.asyncio
    async def test_tasks_frozen_after_start(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Once work starts the breakdown can no longer be edited."""
        created = await create_repair_order(
            owner_client,
            test_customer,
            test_vehicle,
            tasks=[{"description": "Existing"}],
        )
        ro_id = created["id"]
        await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro_id}/status", params={"status": "APPROVED"}
        )
        await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro_id}/status", params={"status": "IN_PROGRESS"}
        )

        add = await owner_client.post(
            f"{REPAIR_ORDERS_URL}{ro_id}/tasks", json={"description": "Sneaky"}
        )
        assert add.status_code == 400

        task_id = created["tasks"][0]["id"]
        delete = await owner_client.delete(f"{REPAIR_ORDERS_URL}{ro_id}/tasks/{task_id}")
        assert delete.status_code == 400

    @pytest.mark.asyncio
    async def test_task_status_transition_sets_completed_at(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Completing a task stamps completed_at."""
        created = await create_repair_order(
            owner_client,
            test_customer,
            test_vehicle,
            tasks=[{"description": "Task"}],
        )
        ro_id = created["id"]
        await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro_id}/status", params={"status": "APPROVED"}
        )
        await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro_id}/status", params={"status": "IN_PROGRESS"}
        )
        task_id = created["tasks"][0]["id"]
        response = await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro_id}/tasks/{task_id}/status",
            params={"status": "COMPLETED"},
        )
        assert response.status_code == 200
        assert response.json()["tasks"][0]["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_invalid_task_status(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """An unknown task status is rejected."""
        created = await create_repair_order(
            owner_client,
            test_customer,
            test_vehicle,
            tasks=[{"description": "Task"}],
        )
        task_id = created["tasks"][0]["id"]
        response = await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{created['id']}/tasks/{task_id}/status",
            params={"status": "BOGUS"},
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_task_update_blocked_on_terminal_ro(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A cancelled RO rejects task status changes."""
        created = await create_repair_order(
            owner_client,
            test_customer,
            test_vehicle,
            tasks=[{"description": "Task"}],
        )
        ro_id = created["id"]
        task_id = created["tasks"][0]["id"]
        await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro_id}/status", params={"status": "CANCELLED"}
        )
        response = await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro_id}/tasks/{task_id}/status",
            params={"status": "COMPLETED"},
        )
        assert response.status_code == 400


class TestRepairOrderSummaryAndDeletion:
    @pytest.mark.asyncio
    async def test_summary_counts(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Summary reports task counts by state."""
        created = await create_repair_order(
            owner_client,
            test_customer,
            test_vehicle,
            tasks=[{"description": "A"}, {"description": "B"}, {"description": "C"}],
        )
        ro_id = created["id"]
        task_a = created["tasks"][0]["id"]
        task_b = created["tasks"][1]["id"]
        await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro_id}/status", params={"status": "APPROVED"}
        )
        await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro_id}/tasks/{task_a}/status",
            params={"status": "COMPLETED"},
        )
        await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro_id}/tasks/{task_b}/status",
            params={"status": "SKIPPED"},
        )
        response = await owner_client.get(f"{REPAIR_ORDERS_URL}{ro_id}/summary")
        assert response.status_code == 200
        body = response.json()
        assert body["counts"]["total"] == 3
        assert body["counts"]["completed"] == 1
        assert body["counts"]["skipped"] == 1
        assert body["counts"]["pending"] == 1
        assert body["tasks_editable"] is True

    @pytest.mark.asyncio
    async def test_delete_draft_ro(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A draft RO can be deleted."""
        created = await create_repair_order(owner_client, test_customer, test_vehicle)
        response = await owner_client.delete(f"{REPAIR_ORDERS_URL}{created['id']}")
        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_delete_blocked_once_worked(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """An RO that has been worked on cannot be deleted."""
        created = await create_repair_order(
            owner_client,
            test_customer,
            test_vehicle,
            tasks=[{"description": "Task"}],
        )
        ro_id = created["id"]
        await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro_id}/status", params={"status": "APPROVED"}
        )
        await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro_id}/status", params={"status": "IN_PROGRESS"}
        )
        response = await owner_client.delete(f"{REPAIR_ORDERS_URL}{ro_id}")
        assert response.status_code == 400


class TestRepairOrderRbac:
    @pytest.mark.asyncio
    async def test_customer_cannot_read_the_shop_repair_order_list(
        self,
        owner_client: AsyncClient,
        customer_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """The customer holds repair_orders:read, and is still refused here.

        This list is the shop's whole job queue. The customer's own repairs are in
        their vehicle history in the portal, which is scoped to the token.
        """
        await create_repair_order(owner_client, test_customer, test_vehicle)
        response = await customer_client.get(REPAIR_ORDERS_URL)
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_customer_cannot_write(
        self,
        customer_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """Customers hold repair_orders:read but not repair_orders:write."""
        response = await customer_client.post(
            REPAIR_ORDERS_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
            },
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_technician_can_read_and_write_tasks(
        self,
        technician_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """Technicians hold repair_orders and tasks read/write."""
        created = await create_repair_order(
            technician_client,
            test_customer,
            test_vehicle,
            tasks=[{"description": "Tech task"}],
        )
        assert len(created["tasks"]) == 1

    @pytest.mark.asyncio
    async def test_parts_staff_denied(
        self,
        parts_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """Parts staff hold no repair-order permissions."""
        response = await parts_client.post(
            REPAIR_ORDERS_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
            },
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_manager_can_manage(
        self, manager_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Service advisors hold repair_orders:manage."""
        created = await create_repair_order(manager_client, test_customer, test_vehicle)
        response = await manager_client.delete(f"{REPAIR_ORDERS_URL}{created['id']}")
        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_unauthenticated_cannot_read(self, unauth_client: AsyncClient):
        """Anonymous callers are rejected."""
        response = await unauth_client.get(REPAIR_ORDERS_URL)
        assert response.status_code == 401
