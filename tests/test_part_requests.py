"""Tests for the technician-to-parts-staff part request workflow.

Covers raising requests, the approve/reject/fulfil state machine, requestability
rules against the parent repair order, and RBAC (in particular that the
requesting technician cannot approve their own request).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.customers.models import Customer
from app.vehicles.models import Vehicle
from tests.factories import VehicleFactory

PART_REQUESTS_URL = "/api/v1/part_requests/"
REPAIR_ORDERS_URL = "/api/v1/repair_orders/"


@pytest.fixture()
async def test_customer(db: AsyncSession):
    customer = Customer(
        first_name="Parts",
        last_name="Customer",
        email="partsreq_test@example.com",
        phone="555-0030",
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


async def create_approved_ro(
    client: AsyncClient, customer: Customer, vehicle: Vehicle
) -> dict:
    """Create a repair order and approve it (requestable state)."""
    create = await client.post(
        REPAIR_ORDERS_URL,
        json={
            "customer_id": str(customer.id),
            "vehicle_id": str(vehicle.id),
            "tasks": [{"description": "Fix thing"}],
        },
    )
    assert create.status_code == 201, create.text
    ro = create.json()
    approve = await client.patch(
        f"{REPAIR_ORDERS_URL}{ro['id']}/status", params={"status": "APPROVED"}
    )
    assert approve.status_code == 200, approve.text
    return approve.json()


async def create_in_progress_ro(
    client: AsyncClient, customer: Customer, vehicle: Vehicle
) -> dict:
    """Create a repair order and start work on it."""
    ro = await create_approved_ro(client, customer, vehicle)
    start = await client.patch(
        f"{REPAIR_ORDERS_URL}{ro['id']}/status", params={"status": "IN_PROGRESS"}
    )
    assert start.status_code == 200, start.text
    return start.json()


class TestPartRequestWorkflow:
    @pytest.mark.asyncio
    async def test_raise_and_approve(
        self, technician_client: AsyncClient, parts_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """A technician raises a request and parts staff approve it."""
        ro = await create_in_progress_ro(technician_client, test_customer, test_vehicle)
        raised = await technician_client.post(
            PART_REQUESTS_URL,
            json={
                "repair_order_id": ro["id"],
                "part_name": "Brake rotor",
                "quantity": 2,
                "reason": "Rusted beyond service",
            },
        )
        assert raised.status_code == 201, raised.text
        request_id = raised.json()["id"]
        assert raised.json()["status"] == "PENDING"

        approved = await parts_client.post(
            f"{PART_REQUESTS_URL}{request_id}/decision",
            json={"decision": "APPROVED", "decision_reason": "in stock"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "APPROVED"
        assert approved.json()["decision_reason"] == "in stock"

    @pytest.mark.asyncio
    async def test_reject_request(
        self, technician_client: AsyncClient, parts_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """Parts staff can reject a request."""
        ro = await create_in_progress_ro(technician_client, test_customer, test_vehicle)
        raised = await technician_client.post(
            PART_REQUESTS_URL,
            json={
                "repair_order_id": ro["id"],
                "part_name": "Bogus part",
                "reason": "thought it was needed",
            },
        )
        rejected = await parts_client.post(
            f"{PART_REQUESTS_URL}{raised.json()['id']}/decision",
            json={"decision": "REJECTED", "decision_reason": "not a real part"},
        )
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "REJECTED"

    @pytest.mark.asyncio
    async def test_fulfil_after_approval(
        self, technician_client: AsyncClient, parts_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """An approved request can be fulfilled."""
        ro = await create_in_progress_ro(technician_client, test_customer, test_vehicle)
        raised = await technician_client.post(
            PART_REQUESTS_URL,
            json={
                "repair_order_id": ro["id"],
                "part_name": "Pad set",
                "reason": "required",
            },
        )
        request_id = raised.json()["id"]
        await parts_client.post(
            f"{PART_REQUESTS_URL}{request_id}/decision", json={"decision": "APPROVED"}
        )
        fulfilled = await parts_client.post(f"{PART_REQUESTS_URL}{request_id}/fulfil")
        assert fulfilled.status_code == 200
        assert fulfilled.json()["status"] == "FULFILLED"

    @pytest.mark.asyncio
    async def test_cannot_fulfil_before_approval(
        self, technician_client: AsyncClient, parts_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """A pending request cannot jump straight to fulfilled."""
        ro = await create_in_progress_ro(technician_client, test_customer, test_vehicle)
        raised = await technician_client.post(
            PART_REQUESTS_URL,
            json={
                "repair_order_id": ro["id"],
                "part_name": "Part",
                "reason": "needed",
            },
        )
        response = await parts_client.post(
            f"{PART_REQUESTS_URL}{raised.json()['id']}/fulfil"
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_rejected_is_terminal(
        self, technician_client: AsyncClient, parts_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """A rejected request accepts no further decisions."""
        ro = await create_in_progress_ro(technician_client, test_customer, test_vehicle)
        raised = await technician_client.post(
            PART_REQUESTS_URL,
            json={"repair_order_id": ro["id"], "part_name": "P", "reason": "r"},
        )
        request_id = raised.json()["id"]
        await parts_client.post(
            f"{PART_REQUESTS_URL}{request_id}/decision", json={"decision": "REJECTED"}
        )
        again = await parts_client.post(
            f"{PART_REQUESTS_URL}{request_id}/decision", json={"decision": "APPROVED"}
        )
        assert again.status_code == 400

    @pytest.mark.asyncio
    async def test_cancel_pending_request(
        self, technician_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """The requesting technician can withdraw a pending request."""
        ro = await create_in_progress_ro(technician_client, test_customer, test_vehicle)
        raised = await technician_client.post(
            PART_REQUESTS_URL,
            json={"repair_order_id": ro["id"], "part_name": "P", "reason": "r"},
        )
        cancelled = await technician_client.post(
            f"{PART_REQUESTS_URL}{raised.json()['id']}/cancel",
            params={"reason": "found in stock"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "CANCELLED"

    @pytest.mark.asyncio
    async def test_cannot_request_before_approval(
        self, technician_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Parts cannot be requested on a DRAFT order."""
        create = await technician_client.post(
            REPAIR_ORDERS_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "tasks": [{"description": "Task"}],
            },
        )
        ro = create.json()
        response = await technician_client.post(
            PART_REQUESTS_URL,
            json={"repair_order_id": ro["id"], "part_name": "P", "reason": "r"},
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_cannot_request_after_completion(
        self, technician_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Parts cannot be requested once the order is completed."""
        ro = await create_in_progress_ro(technician_client, test_customer, test_vehicle)
        await technician_client.patch(
            f"{REPAIR_ORDERS_URL}{ro['id']}/tasks/{ro['tasks'][0]['id']}/status",
            params={"status": "COMPLETED"},
        )
        await technician_client.patch(
            f"{REPAIR_ORDERS_URL}{ro['id']}/status", params={"status": "COMPLETED"}
        )
        response = await technician_client.post(
            PART_REQUESTS_URL,
            json={"repair_order_id": ro["id"], "part_name": "P", "reason": "r"},
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_update_pending_request(
        self, technician_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A pending request can be edited."""
        ro = await create_in_progress_ro(technician_client, test_customer, test_vehicle)
        raised = await technician_client.post(
            PART_REQUESTS_URL,
            json={"repair_order_id": ro["id"], "part_name": "Old", "reason": "r"},
        )
        updated = await technician_client.patch(
            f"{PART_REQUESTS_URL}{raised.json()['id']}", json={"quantity": 4}
        )
        assert updated.status_code == 200
        assert updated.json()["quantity"] == 4.0

    @pytest.mark.asyncio
    async def test_delete_pending_request(
        self, technician_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A pending request can be deleted."""
        ro = await create_in_progress_ro(technician_client, test_customer, test_vehicle)
        raised = await technician_client.post(
            PART_REQUESTS_URL,
            json={"repair_order_id": ro["id"], "part_name": "P", "reason": "r"},
        )
        response = await technician_client.delete(
            f"{PART_REQUESTS_URL}{raised.json()['id']}"
        )
        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_invalid_decision_rejected(
        self, technician_client: AsyncClient, parts_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """An invalid decision value is rejected."""
        ro = await create_in_progress_ro(technician_client, test_customer, test_vehicle)
        raised = await technician_client.post(
            PART_REQUESTS_URL,
            json={"repair_order_id": ro["id"], "part_name": "P", "reason": "r"},
        )
        response = await parts_client.post(
            f"{PART_REQUESTS_URL}{raised.json()['id']}/decision",
            json={"decision": "FULFILLED"},
        )
        assert response.status_code == 422


class TestPartRequestRbac:
    @pytest.mark.asyncio
    async def test_technician_cannot_approve_own_request(
        self, technician_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A technician lacks part_requests:approve and cannot decide."""
        ro = await create_in_progress_ro(technician_client, test_customer, test_vehicle)
        raised = await technician_client.post(
            PART_REQUESTS_URL,
            json={"repair_order_id": ro["id"], "part_name": "P", "reason": "r"},
        )
        response = await technician_client.post(
            f"{PART_REQUESTS_URL}{raised.json()['id']}/decision",
            json={"decision": "APPROVED"},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_parts_staff_can_read_queue(
        self, parts_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Parts staff can read the request queue."""
        response = await parts_client.get(PART_REQUESTS_URL)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_parts_staff_cannot_raise_request(
        self, parts_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Parts staff hold approve but not write on requests."""
        response = await parts_client.post(
            PART_REQUESTS_URL,
            json={
                "repair_order_id": "00000000-0000-0000-0000-000000000000",
                "part_name": "P",
                "reason": "r",
            },
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_customer_denied(
        self, customer_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Customers hold no part-request permissions."""
        response = await customer_client.get(PART_REQUESTS_URL)
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_unauthenticated_cannot_read(self, unauth_client: AsyncClient):
        """Anonymous callers are rejected."""
        response = await unauth_client.get(PART_REQUESTS_URL)
        assert response.status_code == 401
