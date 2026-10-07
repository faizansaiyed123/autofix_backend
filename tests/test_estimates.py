# ruff: noqa: DTZ011
"""Tests for estimate management and customer approval.

Covers estimate CRUD, money calculation (labor, parts, discounts, tax), the
advisor status machine, expiry, the per-line customer approval workflow
including partial approval, and RBAC.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.customers.models import Customer
from app.inspections.models import Inspection
from app.service_requests.models import ServiceRequest
from app.vehicles.models import Vehicle
from tests.factories import VehicleFactory

ESTIMATES_URL = "/api/v1/estimates/"

TODAY = date.today()
FUTURE_DATE = (TODAY + timedelta(days=30)).isoformat()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()
# The same value as a real date object, for the raw SQL that re-dates a
# sent estimate: asyncpg rejects an ISO string for a DATE column.
YESTERDAY_DATE = TODAY - timedelta(days=1)

LABOR_LINE = {
    "item_type": "LABOR",
    "description": "Front brake pad replacement",
    "labor_hours": 2,
    "labor_rate": 95,
}
PART_LINE = {
    "item_type": "PART",
    "description": "Brake pad set",
    "part_number": "BP-4471",
    "part_name": "Ceramic pad set",
    "quantity": 2,
    "unit_price": 45,
}
# LABOR_LINE -> 2 x 95 = 190, PART_LINE -> 2 x 45 = 90, subtotal 280.
SUBTOTAL = 280.0


@pytest.fixture()
async def test_customer(db: AsyncSession):
    """Create a customer to hang estimates off."""
    customer = Customer(
        first_name="Estimate",
        last_name="Customer",
        email="estimate_test@example.com",
        phone="555-0007",
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
        last_name="Customer",
        email="estimate_other@example.com",
        phone="555-0008",
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


async def create_estimate(
    client: AsyncClient,
    customer: Customer,
    vehicle: Vehicle,
    **overrides,
) -> dict:
    """Create an estimate through the API and return its JSON body."""
    payload = {
        "customer_id": str(customer.id),
        "vehicle_id": str(vehicle.id),
        "items": [LABOR_LINE, PART_LINE],
    }
    payload.update(overrides)
    response = await client.post(ESTIMATES_URL, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def create_and_send(
    client: AsyncClient, customer: Customer, vehicle: Vehicle, **overrides
) -> dict:
    """Create an estimate and send it to the customer for a decision."""
    created = await create_estimate(client, customer, vehicle, **overrides)
    response = await client.post(f"{ESTIMATES_URL}{created['id']}/send")
    assert response.status_code == 200, response.text
    return response.json()


def decide(decision: str, notes: str | None = None) -> dict:
    """Build the payload for a customer decision on one line."""
    body: dict = {"decision": decision}
    if notes is not None:
        body["notes"] = notes
    return body


class TestEstimateCreation:
    """Tests for creating estimates."""

    @pytest.mark.asyncio
    async def test_create_empty_estimate(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """An estimate can be created with no lines yet."""
        response = await owner_client.post(
            ESTIMATES_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "DRAFT"
        assert data["items"] == []
        assert data["subtotal"] == 0
        assert data["total"] == 0
        assert data["estimate_number"].startswith("EST-")

    @pytest.mark.asyncio
    async def test_estimate_numbers_are_unique(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Two estimates never share a number."""
        first = await create_estimate(owner_client, test_customer, test_vehicle)
        second = await create_estimate(owner_client, test_customer, test_vehicle)
        assert first["estimate_number"] != second["estimate_number"]

    @pytest.mark.asyncio
    async def test_create_calculates_labor_and_parts(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Labor bills hours x rate, parts bill quantity x unit price."""
        data = await create_estimate(owner_client, test_customer, test_vehicle)
        assert len(data["items"]) == 2
        assert data["items"][0]["line_total"] == 190.0
        assert data["items"][1]["line_total"] == 90.0
        assert data["subtotal"] == SUBTOTAL
        assert data["tax_amount"] == 0
        assert data["total"] == SUBTOTAL

    @pytest.mark.asyncio
    async def test_create_applies_discount(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A DISCOUNT line reduces the total rather than adding to it."""
        data = await create_estimate(
            owner_client,
            test_customer,
            test_vehicle,
            items=[
                LABOR_LINE,
                PART_LINE,
                {
                    "item_type": "DISCOUNT",
                    "description": "Shop loyalty discount",
                    "unit_price": 30,
                },
            ],
        )
        assert data["items"][2]["line_total"] == -30.0
        assert data["subtotal"] == SUBTOTAL
        assert data["discount_amount"] == 30.0
        assert data["total"] == 250.0

    @pytest.mark.asyncio
    async def test_create_applies_line_discount(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A per-line discount nets off that line only."""
        data = await create_estimate(
            owner_client,
            test_customer,
            test_vehicle,
            items=[LABOR_LINE, {**PART_LINE, "discount_amount": 10}],
        )
        assert data["items"][1]["line_total"] == 80.0
        assert data["subtotal"] == 270.0

    @pytest.mark.asyncio
    async def test_create_applies_tax(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Tax is charged on the discounted subtotal."""
        data = await create_estimate(
            owner_client,
            test_customer,
            test_vehicle,
            items=[
                LABOR_LINE,
                PART_LINE,
                {"item_type": "DISCOUNT", "description": "Discount", "unit_price": 30},
            ],
            tax_rate=0.1,
        )
        assert data["tax_rate"] == 0.1
        assert data["tax_amount"] == 25.0
        assert data["total"] == 275.0

    @pytest.mark.asyncio
    async def test_discount_cannot_exceed_subtotal(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A discount larger than the work floors the estimate at zero."""
        data = await create_estimate(
            owner_client,
            test_customer,
            test_vehicle,
            items=[
                PART_LINE,
                {
                    "item_type": "DISCOUNT",
                    "description": "Over-generous discount",
                    "unit_price": 500,
                },
            ],
        )
        assert data["discount_amount"] == 90.0
        assert data["total"] == 0.0

    @pytest.mark.asyncio
    async def test_create_unknown_customer_fails(
        self, owner_client: AsyncClient, test_vehicle: Vehicle
    ):
        """An estimate for a non-existent customer is rejected."""
        response = await owner_client.post(
            ESTIMATES_URL,
            json={"customer_id": str(uuid4()), "vehicle_id": str(test_vehicle.id)},
        )
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_create_vehicle_of_another_customer_fails(
        self, owner_client: AsyncClient, test_customer: Customer, other_vehicle: Vehicle
    ):
        """The vehicle must belong to the estimate's customer."""
        response = await owner_client.post(
            ESTIMATES_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(other_vehicle.id),
            },
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_create_with_inspection(
        self,
        owner_client: AsyncClient,
        db: AsyncSession,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """An estimate can be raised straight from an inspection."""
        inspection = Inspection(
            vehicle_id=test_vehicle.id,
            customer_id=test_customer.id,
            mileage=35000,
        )
        db.add(inspection)
        await db.commit()
        await db.refresh(inspection)

        data = await create_estimate(
            owner_client, test_customer, test_vehicle, inspection_id=str(inspection.id)
        )
        assert data["inspection_id"] == str(inspection.id)

    @pytest.mark.asyncio
    async def test_create_with_inspection_of_other_vehicle_fails(
        self,
        owner_client: AsyncClient,
        db: AsyncSession,
        test_customer: Customer,
        test_vehicle: Vehicle,
        other_customer: Customer,
        other_vehicle: Vehicle,
    ):
        """An inspection for a different vehicle cannot be attached."""
        inspection = Inspection(
            vehicle_id=other_vehicle.id,
            customer_id=other_customer.id,
            mileage=100,
        )
        db.add(inspection)
        await db.commit()
        await db.refresh(inspection)

        response = await owner_client.post(
            ESTIMATES_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "inspection_id": str(inspection.id),
            },
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_create_with_service_request(
        self,
        owner_client: AsyncClient,
        db: AsyncSession,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """An estimate can be raised from a service request."""
        request = ServiceRequest(
            customer_id=test_customer.id,
            vehicle_id=test_vehicle.id,
            title="Brakes squealing",
        )
        db.add(request)
        await db.commit()
        await db.refresh(request)

        data = await create_estimate(
            owner_client, test_customer, test_vehicle, service_request_id=str(request.id)
        )
        assert data["service_request_id"] == str(request.id)

    @pytest.mark.asyncio
    async def test_create_with_service_request_of_other_customer_fails(
        self,
        owner_client: AsyncClient,
        db: AsyncSession,
        test_customer: Customer,
        test_vehicle: Vehicle,
        other_customer: Customer,
        other_vehicle: Vehicle,
    ):
        """A service request belonging to someone else cannot be attached."""
        request = ServiceRequest(
            customer_id=other_customer.id,
            vehicle_id=other_vehicle.id,
            title="Someone else's problem",
        )
        db.add(request)
        await db.commit()
        await db.refresh(request)

        response = await owner_client.post(
            ESTIMATES_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "service_request_id": str(request.id),
            },
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_create_with_unknown_service_request_fails(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A non-existent service request is rejected."""
        response = await owner_client.post(
            ESTIMATES_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "service_request_id": str(uuid4()),
            },
        )
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_labor_line_requires_hours_and_rate(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A labor line without hours or rate is rejected at validation."""
        response = await owner_client.post(
            ESTIMATES_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "items": [{"item_type": "LABOR", "description": "Brake job"}],
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_part_line_requires_price(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A part line with no unit price is rejected at validation."""
        response = await owner_client.post(
            ESTIMATES_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "items": [{"item_type": "PART", "description": "Unknown part"}],
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_discount_line_requires_amount(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A discount line with no amount is rejected at validation."""
        response = await owner_client.post(
            ESTIMATES_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "items": [{"item_type": "DISCOUNT", "description": "Goodwill"}],
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_item_type_rejected(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """An unknown item type is rejected."""
        response = await owner_client.post(
            ESTIMATES_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "items": [{"item_type": "GADGET", "description": "Nope"}],
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_negative_price_rejected(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A negative unit price is rejected."""
        response = await owner_client.post(
            ESTIMATES_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "items": [{**PART_LINE, "unit_price": -5}],
            },
        )
        assert response.status_code == 422


class TestEstimateReads:
    """Tests for retrieving and listing estimates."""

    @pytest.mark.asyncio
    async def test_get_estimate(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A created estimate can be fetched with its lines."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        response = await owner_client.get(f"{ESTIMATES_URL}{created['id']}")
        assert response.status_code == 200
        assert response.json()["id"] == created["id"]

    @pytest.mark.asyncio
    async def test_get_unknown_estimate_returns_404(self, owner_client: AsyncClient):
        """An unknown estimate id is a 404."""
        response = await owner_client.get(f"{ESTIMATES_URL}{uuid4()}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_list_estimates(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Estimates are listed with pagination metadata."""
        await create_estimate(owner_client, test_customer, test_vehicle)
        await create_estimate(owner_client, test_customer, test_vehicle)
        response = await owner_client.get(ESTIMATES_URL)
        assert response.status_code == 200
        body = response.json()
        assert body["meta"]["total"] == 2
        assert len(body["data"]) == 2

    @pytest.mark.asyncio
    async def test_list_filter_by_status(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Status filtering narrows the list."""
        await create_estimate(owner_client, test_customer, test_vehicle)
        await create_estimate(owner_client, test_customer, test_vehicle)
        response = await owner_client.get(f"{ESTIMATES_URL}?status=SENT")
        assert response.json()["meta"]["total"] == 0
        response = await owner_client.get(f"{ESTIMATES_URL}?status=DRAFT")
        assert response.json()["meta"]["total"] == 2

    @pytest.mark.asyncio
    async def test_list_filter_by_customer(
        self,
        owner_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
        other_customer: Customer,
        other_vehicle: Vehicle,
    ):
        """Customer filtering returns only that customer's estimates."""
        await create_estimate(owner_client, test_customer, test_vehicle)
        await create_estimate(owner_client, other_customer, other_vehicle)
        response = await owner_client.get(f"{ESTIMATES_URL}?customer_id={test_customer.id}")
        body = response.json()
        assert body["meta"]["total"] == 1
        assert body["data"][0]["customer_id"] == str(test_customer.id)


class TestEstimateUpdates:
    """Tests for editing estimates and their lines."""

    @pytest.mark.asyncio
    async def test_update_tax_rate_recalculates(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Changing the tax rate recomputes the totals."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        response = await owner_client.patch(
            f"{ESTIMATES_URL}{created['id']}", json={"tax_rate": 0.075}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["tax_amount"] == 21.0
        assert data["total"] == 301.0

    @pytest.mark.asyncio
    async def test_update_rejects_invalid_tax_rate(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A tax rate above 100% is rejected."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        response = await owner_client.patch(
            f"{ESTIMATES_URL}{created['id']}", json={"tax_rate": 1.5}
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_update_valid_until(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """The expiry date can be set on a draft."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        response = await owner_client.patch(
            f"{ESTIMATES_URL}{created['id']}", json={"valid_until": FUTURE_DATE}
        )
        assert response.json()["valid_until"] == FUTURE_DATE
        assert response.json()["is_expired"] is False

    @pytest.mark.asyncio
    async def test_add_item(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A line can be appended and the totals follow."""
        created = await create_estimate(owner_client, test_customer, test_vehicle, items=[])
        response = await owner_client.post(
            f"{ESTIMATES_URL}{created['id']}/items", json=LABOR_LINE
        )
        assert response.status_code == 201
        data = response.json()
        assert len(data["items"]) == 1
        assert data["total"] == 190.0

    @pytest.mark.asyncio
    async def test_update_item_recalculates(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Editing a line's price updates the estimate total."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        item_id = created["items"][0]["id"]
        response = await owner_client.patch(
            f"{ESTIMATES_URL}{created['id']}/items/{item_id}",
            json={"labor_hours": 3, "labor_rate": 100},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["items"][0]["line_total"] == 300.0
        assert data["subtotal"] == 390.0

    @pytest.mark.asyncio
    async def test_update_part_price_to_zero_rejected(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Zeroing a part price is a business-rule failure, not a silent zero line."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        item_id = created["items"][1]["id"]
        response = await owner_client.patch(
            f"{ESTIMATES_URL}{created['id']}/items/{item_id}", json={"unit_price": 0}
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_delete_item(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Deleting a line removes it and recalculates."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        item_id = created["items"][0]["id"]
        response = await owner_client.delete(
            f"{ESTIMATES_URL}{created['id']}/items/{item_id}"
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        assert data["subtotal"] == 90.0

    @pytest.mark.asyncio
    async def test_delete_unknown_item_returns_404(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Deleting a line that is not on the estimate is a 404."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        response = await owner_client.delete(
            f"{ESTIMATES_URL}{created['id']}/items/{uuid4()}"
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_estimate(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A draft estimate can be deleted outright."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        response = await owner_client.delete(f"{ESTIMATES_URL}{created['id']}")
        assert response.status_code == 204
        assert (await owner_client.get(f"{ESTIMATES_URL}{created['id']}")).status_code == 404


class TestEstimateStatus:
    """Tests for the advisor-driven status machine."""

    @pytest.mark.asyncio
    async def test_send_estimate(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Sending moves a draft to SENT and stamps sent_at."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        response = await owner_client.post(f"{ESTIMATES_URL}{created['id']}/send")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "SENT"
        assert data["sent_at"] is not None

    @pytest.mark.asyncio
    async def test_send_without_items_fails(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """An empty estimate cannot be sent."""
        created = await create_estimate(owner_client, test_customer, test_vehicle, items=[])
        response = await owner_client.post(f"{ESTIMATES_URL}{created['id']}/send")
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_send_expired_estimate_fails(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """An estimate that already expired cannot be sent."""
        created = await create_estimate(
            owner_client, test_customer, test_vehicle, valid_until=YESTERDAY
        )
        response = await owner_client.post(f"{ESTIMATES_URL}{created['id']}/send")
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_draft_cannot_jump_to_approved(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """The status machine refuses a skipped step."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        response = await owner_client.patch(
            f"{ESTIMATES_URL}{created['id']}/status?status=APPROVED"
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_status_rejected(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """An unknown status value is rejected."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        response = await owner_client.patch(
            f"{ESTIMATES_URL}{created['id']}/status?status=WAT"
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_edit_blocked_after_send(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Lines are frozen once the customer has the estimate."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        await owner_client.post(f"{ESTIMATES_URL}{created['id']}/send")
        response = await owner_client.post(
            f"{ESTIMATES_URL}{created['id']}/items", json=LABOR_LINE
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_update_blocked_after_send(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """The header is frozen once the estimate has been sent."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        await owner_client.post(f"{ESTIMATES_URL}{created['id']}/send")
        response = await owner_client.patch(
            f"{ESTIMATES_URL}{created['id']}", json={"tax_rate": 0.2}
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_delete_blocked_after_send(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A sent estimate must be cancelled, not deleted."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        await owner_client.post(f"{ESTIMATES_URL}{created['id']}/send")
        response = await owner_client.delete(f"{ESTIMATES_URL}{created['id']}")
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_cancel_sent_estimate(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A sent estimate can be cancelled with a reason."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        await owner_client.post(f"{ESTIMATES_URL}{created['id']}/send")
        response = await owner_client.post(
            f"{ESTIMATES_URL}{created['id']}/cancel?reason=Customer+went+elsewhere"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "CANCELLED"
        assert data["decline_reason"] == "Customer went elsewhere"


class TestCustomerApproval:
    """The per-line decision workflow, and the rules behind it.

    The decisions here are recorded by a service advisor rather than by the
    customer token, because these tests are about the *rules* — one line at a
    time, discounts counting towards the approved total, a line that can only be
    decided once, a sent estimate that cannot be decided after it expires. Those
    are the shop's rules and they apply whoever taps the button: the counter, or
    the portal, which calls this same ``decide_item``.

    That the customer can make the decision *for themselves* is a separate
    question, and it is answered in ``test_portal.py`` against the portal's own
    endpoint. Here, the customer token is not used at all — the shop's endpoints
    are behind the staff boundary, and ``test_staff_access_boundary.py`` is what
    says so.
    """

    @pytest.mark.asyncio
    async def test_customer_approves_whole_estimate(
        self,
        owner_client: AsyncClient,
        manager_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """Approving every line moves the estimate to APPROVED."""
        sent = await create_and_send(owner_client, test_customer, test_vehicle)
        for item in sent["items"]:
            response = await manager_client.post(
                f"{ESTIMATES_URL}{sent['id']}/items/{item['id']}/decision",
                json=decide("APPROVED"),
            )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "APPROVED"
        assert all(i["status"] == "APPROVED" for i in data["items"])
        assert data["decided_at"] is not None

    @pytest.mark.asyncio
    async def test_partial_approval_then_full(
        self,
        owner_client: AsyncClient,
        manager_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """One approved line plus one pending leaves it PARTIALLY_APPROVED."""
        sent = await create_and_send(owner_client, test_customer, test_vehicle)
        first, second = sent["items"]

        response = await manager_client.post(
            f"{ESTIMATES_URL}{sent['id']}/items/{first['id']}/decision",
            json=decide("APPROVED", "Yes please"),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "PARTIALLY_APPROVED"
        assert response.json()["items"][0]["customer_notes"] == "Yes please"

        response = await manager_client.post(
            f"{ESTIMATES_URL}{sent['id']}/items/{second['id']}/decision",
            json=decide("APPROVED"),
        )
        assert response.json()["status"] == "APPROVED"

    @pytest.mark.asyncio
    async def test_declining_everything(
        self,
        owner_client: AsyncClient,
        manager_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """Declining every line moves the estimate to DECLINED."""
        sent = await create_and_send(owner_client, test_customer, test_vehicle)
        for item in sent["items"]:
            response = await manager_client.post(
                f"{ESTIMATES_URL}{sent['id']}/items/{item['id']}/decision",
                json=decide("DECLINED"),
            )
        assert response.json()["status"] == "DECLINED"
        assert response.json()["approved_total"] == 0

    @pytest.mark.asyncio
    async def test_approved_total_reflects_approved_lines(
        self,
        owner_client: AsyncClient,
        manager_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """Only approved lines count towards the approved total."""
        sent = await create_and_send(owner_client, test_customer, test_vehicle)
        labor, part = sent["items"]
        await manager_client.post(
            f"{ESTIMATES_URL}{sent['id']}/items/{labor['id']}/decision",
            json=decide("APPROVED"),
        )
        response = await manager_client.post(
            f"{ESTIMATES_URL}{sent['id']}/items/{part['id']}/decision",
            json=decide("DECLINED"),
        )
        data = response.json()
        assert data["status"] == "APPROVED"
        assert data["approved_total"] == 190.0
        assert data["total"] == SUBTOTAL

    @pytest.mark.asyncio
    async def test_approved_total_includes_approved_discount(
        self,
        owner_client: AsyncClient,
        manager_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """An approved discount comes off the approved total."""
        sent = await create_and_send(
            owner_client,
            test_customer,
            test_vehicle,
            items=[
                PART_LINE,
                {"item_type": "DISCOUNT", "description": "Discount", "unit_price": 40},
            ],
        )
        for item in sent["items"]:
            await manager_client.post(
                f"{ESTIMATES_URL}{sent['id']}/items/{item['id']}/decision",
                json=decide("APPROVED"),
            )
        response = await manager_client.get(f"{ESTIMATES_URL}{sent['id']}")
        # 90 of parts less the approved 40 discount.
        assert response.json()["approved_total"] == 50.0

    @pytest.mark.asyncio
    async def test_deciding_twice_conflicts(
        self,
        owner_client: AsyncClient,
        manager_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """A line can only be decided once."""
        sent = await create_and_send(owner_client, test_customer, test_vehicle)
        item = sent["items"][0]
        await manager_client.post(
            f"{ESTIMATES_URL}{sent['id']}/items/{item['id']}/decision",
            json=decide("APPROVED"),
        )
        response = await manager_client.post(
            f"{ESTIMATES_URL}{sent['id']}/items/{item['id']}/decision",
            json=decide("DECLINED"),
        )
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_cannot_decide_draft(
        self,
        owner_client: AsyncClient,
        manager_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """A draft estimate is not open for decisions."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        item = created["items"][0]
        response = await manager_client.post(
            f"{ESTIMATES_URL}{created['id']}/items/{item['id']}/decision",
            json=decide("APPROVED"),
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_cannot_decide_cancelled(
        self,
        owner_client: AsyncClient,
        manager_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """A cancelled estimate is closed to decisions."""
        sent = await create_and_send(owner_client, test_customer, test_vehicle)
        await owner_client.post(f"{ESTIMATES_URL}{sent['id']}/cancel")
        item = sent["items"][0]
        response = await manager_client.post(
            f"{ESTIMATES_URL}{sent['id']}/items/{item['id']}/decision",
            json=decide("APPROVED"),
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_expire_then_decide_fails(
        self,
        owner_client: AsyncClient,
        manager_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """Once an estimate is EXPIRED the customer cannot decide it."""
        sent = await create_and_send(owner_client, test_customer, test_vehicle)
        item = sent["items"][0]
        expired = await owner_client.patch(
            f"{ESTIMATES_URL}{sent['id']}/status?status=EXPIRED"
        )
        assert expired.status_code == 200
        assert expired.json()["status"] == "EXPIRED"

        response = await manager_client.post(
            f"{ESTIMATES_URL}{sent['id']}/items/{item['id']}/decision",
            json=decide("APPROVED"),
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_lapsed_valid_until_expires_on_decision(
        self,
        db: AsyncSession,
        owner_client: AsyncClient,
        manager_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """An estimate whose valid_until has lapsed expires and refuses the decision.

        The date is moved backwards directly in the database: a sent estimate
        can no longer be re-dated through the API, and waiting a month is not an
        option in a test.
        """
        sent = await create_and_send(
            owner_client, test_customer, test_vehicle, valid_until=FUTURE_DATE
        )
        item = sent["items"][0]

        await db.execute(
            text("UPDATE estimates SET valid_until = :d WHERE id = :i"),
            {"d": YESTERDAY_DATE, "i": sent["id"]},
        )
        await db.commit()
        db.expire_all()

        response = await manager_client.post(
            f"{ESTIMATES_URL}{sent['id']}/items/{item['id']}/decision",
            json=decide("APPROVED"),
        )
        assert response.status_code == 400
        assert "expired" in response.json()["detail"].lower()

        # The lazy expiry is persisted, so the estimate stops looking open.
        after = await manager_client.get(f"{ESTIMATES_URL}{sent['id']}")
        assert after.json()["status"] == "EXPIRED"
        assert after.json()["is_expired"] is True

    @pytest.mark.asyncio
    async def test_manager_holds_approve_permission(
        self, manager_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A service advisor may record a decision the customer phoned in.

        The role config grants SERVICE_ADVISOR estimates:approve so front-desk
        staff can capture a verbal approval; it is the roles without that
        permission (technician, parts staff) that are locked out.
        """
        sent = await create_and_send(manager_client, test_customer, test_vehicle)
        item = sent["items"][0]
        response = await manager_client.post(
            f"{ESTIMATES_URL}{sent['id']}/items/{item['id']}/decision",
            json=decide("APPROVED", "Customer approved by phone"),
        )
        assert response.status_code == 200
        assert response.json()["items"][0]["customer_notes"] == "Customer approved by phone"

    @pytest.mark.asyncio
    async def test_invalid_decision_rejected(
        self,
        owner_client: AsyncClient,
        manager_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """A decision must be APPROVED or DECLINED."""
        sent = await create_and_send(owner_client, test_customer, test_vehicle)
        item = sent["items"][0]
        response = await manager_client.post(
            f"{ESTIMATES_URL}{sent['id']}/items/{item['id']}/decision",
            json=decide("MAYBE"),
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_pending_decision_rejected(
        self,
        owner_client: AsyncClient,
        manager_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """PENDING is not a decision the customer can submit."""
        sent = await create_and_send(owner_client, test_customer, test_vehicle)
        item = sent["items"][0]
        response = await manager_client.post(
            f"{ESTIMATES_URL}{sent['id']}/items/{item['id']}/decision",
            json=decide("PENDING"),
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_decide_unknown_item_returns_404(
        self,
        owner_client: AsyncClient,
        manager_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """Deciding a line that is not on the estimate is a 404."""
        sent = await create_and_send(owner_client, test_customer, test_vehicle)
        response = await manager_client.post(
            f"{ESTIMATES_URL}{sent['id']}/items/{uuid4()}/decision",
            json={"decision": "APPROVED"},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_summary_reflects_decisions(
        self,
        owner_client: AsyncClient,
        manager_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """The summary endpoint reports per-decision counts and money."""
        sent = await create_and_send(owner_client, test_customer, test_vehicle)
        item = sent["items"][0]
        await manager_client.post(
            f"{ESTIMATES_URL}{sent['id']}/items/{item['id']}/decision",
            json=decide("APPROVED"),
        )
        response = await manager_client.get(f"{ESTIMATES_URL}{sent['id']}/summary")
        assert response.status_code == 200
        data = response.json()
        assert data["counts"] == {"total": 2, "pending": 1, "approved": 1, "declined": 0}
        assert data["totals"]["total"] == SUBTOTAL
        assert data["totals"]["approved_total"] == 190.0
        assert data["can_decide"] is True

    @pytest.mark.asyncio
    async def test_summary_of_unknown_estimate_404(self, owner_client: AsyncClient):
        """The summary of a missing estimate is a 404."""
        response = await owner_client.get(f"{ESTIMATES_URL}{uuid4()}/summary")
        assert response.status_code == 404


class TestEstimateRBAC:
    """Tests for estimate authorization."""

    @pytest.mark.asyncio
    async def test_customer_cannot_create(
        self, customer_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Customers read and approve estimates but never author them."""
        response = await customer_client.post(
            ESTIMATES_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
            },
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_customer_cannot_send(
        self,
        owner_client: AsyncClient,
        customer_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """A customer cannot send their own estimate."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        response = await customer_client.post(f"{ESTIMATES_URL}{created['id']}/send")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_customer_cannot_delete(
        self,
        owner_client: AsyncClient,
        customer_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """A customer cannot delete an estimate."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        response = await customer_client.delete(f"{ESTIMATES_URL}{created['id']}")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_customer_cannot_read_the_shop_estimate(
        self,
        owner_client: AsyncClient,
        customer_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """The customer holds estimates:read, and is still refused here.

        This endpoint serves the shop: an estimate id is not a customer's to read
        simply because the shop holds one for them, and the shop's list is every
        estimate it has written. The customer's own estimate is in the portal,
        where the id is checked against the account the token belongs to.
        """
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        response = await customer_client.get(f"{ESTIMATES_URL}{created['id']}")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_customer_cannot_read_the_shop_summary(
        self,
        owner_client: AsyncClient,
        customer_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """The money summary is shop arithmetic over shop data, and it stays there."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        response = await customer_client.get(f"{ESTIMATES_URL}{created['id']}/summary")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_technician_cannot_read(
        self,
        owner_client: AsyncClient,
        technician_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """Technicians hold no estimate permissions."""
        created = await create_estimate(owner_client, test_customer, test_vehicle)
        response = await technician_client.get(f"{ESTIMATES_URL}{created['id']}")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_technician_cannot_create(
        self, technician_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Technicians cannot author estimates."""
        response = await technician_client.post(
            ESTIMATES_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
            },
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_technician_cannot_approve(
        self,
        owner_client: AsyncClient,
        technician_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """Technicians hold neither estimates:read nor estimates:approve."""
        sent = await create_and_send(owner_client, test_customer, test_vehicle)
        item = sent["items"][0]
        response = await technician_client.post(
            f"{ESTIMATES_URL}{sent['id']}/items/{item['id']}/decision",
            json=decide("APPROVED"),
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_parts_staff_cannot_approve(
        self,
        owner_client: AsyncClient,
        parts_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """Parts staff hold no estimate permissions."""
        sent = await create_and_send(owner_client, test_customer, test_vehicle)
        item = sent["items"][0]
        response = await parts_client.post(
            f"{ESTIMATES_URL}{sent['id']}/items/{item['id']}/decision",
            json=decide("APPROVED"),
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_manager_can_create_and_send(
        self, manager_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Service advisors author and send estimates."""
        created = await create_estimate(manager_client, test_customer, test_vehicle)
        response = await manager_client.post(f"{ESTIMATES_URL}{created['id']}/send")
        assert response.status_code == 200
        assert response.json()["status"] == "SENT"

    @pytest.mark.asyncio
    async def test_unauthenticated_cannot_read(self, unauth_client: AsyncClient):
        """Anonymous callers are rejected."""
        response = await unauth_client.get(ESTIMATES_URL)
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_owner_can_approve(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """The owner holds every permission, including approval."""
        sent = await create_and_send(owner_client, test_customer, test_vehicle)
        item = sent["items"][0]
        response = await owner_client.post(
            f"{ESTIMATES_URL}{sent['id']}/items/{item['id']}/decision",
            json=decide("APPROVED"),
        )
        assert response.status_code == 200
