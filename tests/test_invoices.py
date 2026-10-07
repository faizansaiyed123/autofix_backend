# ruff: noqa: DTZ011
# The API returns the shop's local calendar date (an invoice written after
# midnight must not carry yesterday's date because the server runs on UTC), so
# these assertions compare against the same local `date.today()`. Ruff would
# rather they used a UTC date, which would disagree with what the API returns.
"""Tests for invoicing.

Covers raising an invoice against finished work, the freezing of approved
estimate lines onto the bill, shop-added lines, the derived money totals, the
draft/issued/paid lifecycle, writing an invoice off, the queries the shop chases
money with, database-level integrity, and RBAC.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import BusinessRuleError
from app.customers.models import Customer
from app.invoices.models import Invoice, InvoiceItem, InvoiceStatus
from app.invoices.schemas import InvoiceUpdate
from app.invoices.services import InvoiceService
from app.vehicles.models import Vehicle
from tests.factories import VehicleFactory

INVOICES_URL = "/api/v1/invoices/"
ESTIMATES_URL = "/api/v1/estimates/"
RO_URL = "/api/v1/repair_orders/"

# Two approved lines: 2h labour at 95.00 and one part at 45.00 -> 235.00.
DEFAULT_ESTIMATE_ITEMS = [
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
        "part_name": "Ceramic pad set",
        "quantity": 1,
        "unit_price": 45,
    },
]


@pytest.fixture()
async def test_customer(db: AsyncSession):
    """A customer to hang the work off."""
    customer = Customer(
        first_name="Invoice",
        last_name="Customer",
        email="invoice_test@example.com",
        phone="555-0200",
        preferred_contact="EMAIL",
        customer_status="ACTIVE",
    )
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    return customer


@pytest.fixture()
async def test_vehicle(db: AsyncSession, test_customer: Customer):
    """A vehicle owned by the test customer."""
    vehicle = VehicleFactory.build(customer_id=test_customer.id)
    db.add(vehicle)
    await db.commit()
    await db.refresh(vehicle)
    return vehicle


# --- helpers ---------------------------------------------------------------


async def create_approved_estimate(
    client: AsyncClient,
    customer: Customer,
    vehicle: Vehicle,
    items: list[dict] | None = None,
    tax_rate: float = 0.0,
) -> dict:
    """Create an estimate the customer has approved line by line."""
    create = await client.post(
        ESTIMATES_URL,
        json={
            "customer_id": str(customer.id),
            "vehicle_id": str(vehicle.id),
            "tax_rate": tax_rate,
            "items": items if items is not None else DEFAULT_ESTIMATE_ITEMS,
        },
    )
    assert create.status_code == 201, create.text
    estimate = create.json()

    send = await client.post(f"{ESTIMATES_URL}{estimate['id']}/send")
    assert send.status_code == 200, send.text

    current = send.json()
    for item in current["items"]:
        decision = await client.post(
            f"{ESTIMATES_URL}{current['id']}/items/{item['id']}/decision",
            json={"decision": "APPROVED"},
        )
        assert decision.status_code == 200, decision.text
    return decision.json()


async def create_repair_order(
    client: AsyncClient,
    customer: Customer,
    vehicle: Vehicle,
    estimate_id: str | None = None,
) -> dict:
    """Create a repair order, inheriting its breakdown from the estimate."""
    payload: dict = {
        "customer_id": str(customer.id),
        "vehicle_id": str(vehicle.id),
    }
    if estimate_id:
        payload["estimate_id"] = estimate_id
    else:
        payload["tasks"] = [{"description": "Replace brake pads"}]
    response = await client.post(RO_URL, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def set_ro_status(client: AsyncClient, ro_id: str, status: str) -> dict:
    response = await client.patch(f"{RO_URL}{ro_id}/status", params={"status": status})
    assert response.status_code == 200, response.text
    return response.json()


async def create_billable_ro(
    client: AsyncClient,
    customer: Customer,
    vehicle: Vehicle,
    estimate_id: str | None = None,
) -> dict:
    """Drive a repair order all the way to QC_PASSED, which is what can be billed."""
    ro = await create_repair_order(client, customer, vehicle, estimate_id)
    await set_ro_status(client, ro["id"], "APPROVED")
    await set_ro_status(client, ro["id"], "IN_PROGRESS")
    for task in ro["tasks"]:
        done = await client.patch(
            f"{RO_URL}{ro['id']}/tasks/{task['id']}/status",
            params={"status": "COMPLETED"},
        )
        assert done.status_code == 200, done.text
    await set_ro_status(client, ro["id"], "COMPLETED")
    return await set_ro_status(client, ro["id"], "QC_PASSED")


async def create_invoice(client: AsyncClient, ro: dict, **overrides) -> dict:
    """Raise a draft invoice against a repair order."""
    response = await client.post(
        INVOICES_URL, json={"repair_order_id": ro["id"], **overrides}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def issue_invoice(client: AsyncClient, invoice_id: str) -> dict:
    response = await client.post(f"{INVOICES_URL}{invoice_id}/issue")
    assert response.status_code == 200, response.text
    return response.json()


def manual_line(description: str = "Shop supplies", unit_price: float = 25.0, **extra) -> dict:
    """A shop-raised charge line."""
    return {
        "item_type": "PART",
        "description": description,
        "quantity": 1,
        "unit_price": unit_price,
        **extra,
    }


# --- creation --------------------------------------------------------------


class TestInvoiceCreation:
    @pytest.mark.asyncio
    async def test_create_copies_approved_estimate_lines(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """The bill starts as a frozen copy of what the customer approved."""
        estimate = await create_approved_estimate(
            owner_client, test_customer, test_vehicle
        )
        ro = await create_billable_ro(
            owner_client, test_customer, test_vehicle, estimate["id"]
        )
        invoice = await create_invoice(owner_client, ro)

        assert invoice["status"] == "DRAFT"
        assert invoice["estimate_id"] == estimate["id"]
        assert invoice["customer_id"] == str(test_customer.id)
        assert invoice["vehicle_id"] == str(test_vehicle.id)
        assert len(invoice["items"]) == 2
        assert all(item["source"] == "ESTIMATE" for item in invoice["items"])
        assert invoice["subtotal"] == 235.0
        assert invoice["total"] == 235.0
        assert invoice["balance"] == 235.0
        assert invoice["amount_paid"] == 0.0

    @pytest.mark.asyncio
    async def test_estimate_line_keeps_labor_as_hours_and_rate(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A copied labour line still reads as hours x rate, not one lump sum."""
        estimate = await create_approved_estimate(
            owner_client, test_customer, test_vehicle
        )
        ro = await create_billable_ro(
            owner_client, test_customer, test_vehicle, estimate["id"]
        )
        invoice = await create_invoice(owner_client, ro)

        labor = next(i for i in invoice["items"] if i["item_type"] == "LABOR")
        assert labor["quantity"] == 2.0
        assert labor["unit_price"] == 95.0
        assert labor["line_total"] == 190.0

    @pytest.mark.asyncio
    async def test_only_approved_estimate_lines_are_billed(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A declined line is not a debt and a pending one is not agreed."""
        create = await owner_client.post(
            ESTIMATES_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "items": [
                    {"item_type": "PART", "description": "Wiper blades", "unit_price": 30},
                    {"item_type": "PART", "description": "Air filter", "unit_price": 20},
                    {"item_type": "PART", "description": "Optional polish", "unit_price": 90},
                ],
            },
        )
        assert create.status_code == 201, create.text
        estimate = create.json()
        sent = await owner_client.post(f"{ESTIMATES_URL}{estimate['id']}/send")
        items = sent.json()["items"]

        await owner_client.post(
            f"{ESTIMATES_URL}{estimate['id']}/items/{items[0]['id']}/decision",
            json={"decision": "APPROVED"},
        )
        declined = await owner_client.post(
            f"{ESTIMATES_URL}{estimate['id']}/items/{items[1]['id']}/decision",
            json={"decision": "DECLINED"},
        )
        assert declined.status_code == 200

        ro = await create_billable_ro(
            owner_client, test_customer, test_vehicle, estimate["id"]
        )
        invoice = await create_invoice(owner_client, ro)

        assert [i["description"] for i in invoice["items"]] == ["Wiper blades"]
        assert invoice["total"] == 30.0

    @pytest.mark.asyncio
    async def test_invoice_inherits_the_estimates_tax_rate(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A bill taxes what the estimate taxed unless the desk says otherwise."""
        estimate = await create_approved_estimate(
            owner_client, test_customer, test_vehicle, tax_rate=0.1
        )
        ro = await create_billable_ro(
            owner_client, test_customer, test_vehicle, estimate["id"]
        )
        invoice = await create_invoice(owner_client, ro)

        assert invoice["tax_rate"] == 0.1
        assert invoice["tax_amount"] == 23.5
        assert invoice["total"] == 258.5

    @pytest.mark.asyncio
    async def test_explicit_tax_rate_overrides_the_estimate(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        estimate = await create_approved_estimate(
            owner_client, test_customer, test_vehicle, tax_rate=0.1
        )
        ro = await create_billable_ro(
            owner_client, test_customer, test_vehicle, estimate["id"]
        )
        invoice = await create_invoice(owner_client, ro, tax_rate=0.0)

        assert invoice["tax_rate"] == 0.0
        assert invoice["total"] == 235.0

    @pytest.mark.asyncio
    async def test_create_without_an_estimate_starts_empty(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """An RO with no estimate can still be invoiced for work done off-book."""
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(owner_client, ro)

        assert invoice["items"] == []
        assert invoice["estimate_id"] is None
        assert invoice["total"] == 0.0

    @pytest.mark.asyncio
    async def test_extra_items_are_added_at_creation(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Work the shop did on top of the estimate is billed alongside it."""
        estimate = await create_approved_estimate(
            owner_client, test_customer, test_vehicle
        )
        ro = await create_billable_ro(
            owner_client, test_customer, test_vehicle, estimate["id"]
        )
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line("Roadside callout", 60.0)]
        )

        assert len(invoice["items"]) == 3
        extra = invoice["items"][-1]
        assert extra["source"] == "MANUAL"
        assert extra["line_total"] == 60.0
        assert invoice["total"] == 295.0

    @pytest.mark.asyncio
    async def test_one_invoice_per_repair_order(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """The work was done once, so it is charged once."""
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        await create_invoice(owner_client, ro)

        second = await owner_client.post(
            INVOICES_URL, json={"repair_order_id": ro["id"]}
        )
        assert second.status_code == 409
        assert "already been invoiced" in second.json()["detail"]

    @pytest.mark.asyncio
    async def test_unfinished_work_cannot_be_invoiced(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Only work that has passed quality control is billed."""
        ro = await create_repair_order(owner_client, test_customer, test_vehicle)
        response = await owner_client.post(
            INVOICES_URL, json={"repair_order_id": ro["id"]}
        )
        assert response.status_code == 400
        assert "quality control" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_in_progress_work_cannot_be_invoiced(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_repair_order(owner_client, test_customer, test_vehicle)
        await set_ro_status(owner_client, ro["id"], "APPROVED")
        await set_ro_status(owner_client, ro["id"], "IN_PROGRESS")

        response = await owner_client.post(
            INVOICES_URL, json={"repair_order_id": ro["id"]}
        )
        assert response.status_code == 400
        assert "IN_PROGRESS" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_cancelled_work_cannot_be_invoiced(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_repair_order(owner_client, test_customer, test_vehicle)
        await set_ro_status(owner_client, ro["id"], "CANCELLED")

        response = await owner_client.post(
            INVOICES_URL, json={"repair_order_id": ro["id"]}
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_unknown_repair_order_is_not_found(
        self, owner_client: AsyncClient
    ):
        response = await owner_client.post(
            INVOICES_URL, json={"repair_order_id": "00000000-0000-0000-0000-000000000000"}
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_invoice_number_is_unique(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        first_ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        second_ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        first = await create_invoice(owner_client, first_ro)
        second = await create_invoice(owner_client, second_ro)

        assert first["invoice_number"] != second["invoice_number"]
        assert first["invoice_number"].startswith("INV-")

    @pytest.mark.asyncio
    async def test_due_date_defaults_to_net_thirty(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A bill with no agreed date is still owed on a schedule."""
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(owner_client, ro)

        assert invoice["invoice_date"] == date.today().isoformat()
        assert invoice["due_date"] == (
            date.today() + timedelta(days=30)
        ).isoformat()

    @pytest.mark.asyncio
    async def test_explicit_due_date_is_kept(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        due = (date.today() + timedelta(days=7)).isoformat()
        invoice = await create_invoice(owner_client, ro, due_date=due)

        assert invoice["due_date"] == due

    @pytest.mark.asyncio
    async def test_due_date_cannot_precede_the_invoice_date(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        response = await owner_client.post(
            INVOICES_URL,
            json={
                "repair_order_id": ro["id"],
                "invoice_date": "2026-05-10",
                "due_date": "2026-05-01",
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_records_who_raised_it(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(owner_client, ro)
        assert invoice["created_by_id"] is not None


# --- lines -----------------------------------------------------------------


class TestInvoiceLines:
    @pytest.mark.asyncio
    async def test_add_a_shop_line(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(owner_client, ro)

        response = await owner_client.post(
            f"{INVOICES_URL}{invoice['id']}/items", json=manual_line()
        )
        assert response.status_code == 201
        body = response.json()
        assert body["total"] == 25.0
        assert body["items"][0]["source"] == "MANUAL"

    @pytest.mark.asyncio
    async def test_add_a_discount_line(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A discount is stored positive and always subtracted."""
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(owner_client, ro)
        await owner_client.post(
            f"{INVOICES_URL}{invoice['id']}/items", json=manual_line(unit_price=100.0)
        )

        response = await owner_client.post(
            f"{INVOICES_URL}{invoice['id']}/items",
            json={
                "item_type": "DISCOUNT",
                "description": "Goodwill",
                "quantity": 1,
                "unit_price": 20.0,
            },
        )
        assert response.status_code == 201
        body = response.json()
        discount = next(i for i in body["items"] if i["item_type"] == "DISCOUNT")
        assert discount["line_total"] == -20.0
        assert body["subtotal"] == 100.0
        assert body["discount_amount"] == 20.0
        assert body["total"] == 80.0

    @pytest.mark.asyncio
    async def test_tax_falls_on_the_discounted_amount(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A discount the shop gave reduces the taxable base; it is not taxed."""
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=100.0)]
        )
        await owner_client.post(
            f"{INVOICES_URL}{invoice['id']}/items",
            json={
                "item_type": "DISCOUNT",
                "description": "Courtesy",
                "quantity": 1,
                "unit_price": 20.0,
            },
        )
        response = await owner_client.patch(
            f"{INVOICES_URL}{invoice['id']}", json={"tax_rate": 0.1}
        )
        body = response.json()

        assert body["subtotal"] == 100.0
        assert body["discount_amount"] == 20.0
        assert body["tax_amount"] == 8.0
        assert body["total"] == 88.0

    @pytest.mark.asyncio
    async def test_discounts_cannot_exceed_the_work(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """The bill floors at zero rather than going negative."""
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=50.0)]
        )
        response = await owner_client.post(
            f"{INVOICES_URL}{invoice['id']}/items",
            json={
                "item_type": "DISCOUNT",
                "description": "Over-generous",
                "quantity": 1,
                "unit_price": 500.0,
            },
        )
        body = response.json()
        assert body["subtotal"] == 50.0
        assert body["discount_amount"] == 50.0
        assert body["total"] == 0.0

    @pytest.mark.asyncio
    async def test_line_with_no_price_is_rejected(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(owner_client, ro)

        response = await owner_client.post(
            f"{INVOICES_URL}{invoice['id']}/items",
            json={
                "item_type": "PART",
                "description": "Mystery part",
                "quantity": 1,
                "unit_price": 0,
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_discount_must_be_a_single_amount(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """'10% off' is quoted as the money it saves, not a rate on the line."""
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(owner_client, ro)

        response = await owner_client.post(
            f"{INVOICES_URL}{invoice['id']}/items",
            json={
                "item_type": "DISCOUNT",
                "description": "Two for one",
                "quantity": 2,
                "unit_price": 10.0,
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_update_a_shop_line(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=25.0)]
        )
        line_id = invoice["items"][0]["id"]

        response = await owner_client.patch(
            f"{INVOICES_URL}{invoice['id']}/items/{line_id}",
            json={"unit_price": 40.0, "quantity": 2},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["items"][0]["line_total"] == 80.0
        assert body["total"] == 80.0

    @pytest.mark.asyncio
    async def test_delete_a_shop_line(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=25.0)]
        )
        line_id = invoice["items"][0]["id"]

        response = await owner_client.delete(
            f"{INVOICES_URL}{invoice['id']}/items/{line_id}"
        )
        assert response.status_code == 200
        assert response.json()["items"] == []
        assert response.json()["total"] == 0.0

    @pytest.mark.asyncio
    async def test_estimate_line_cannot_be_edited(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """The copied line is the customer's approval, frozen onto the bill."""
        estimate = await create_approved_estimate(
            owner_client, test_customer, test_vehicle
        )
        ro = await create_billable_ro(
            owner_client, test_customer, test_vehicle, estimate["id"]
        )
        invoice = await create_invoice(owner_client, ro)
        line_id = invoice["items"][0]["id"]

        response = await owner_client.patch(
            f"{INVOICES_URL}{invoice['id']}/items/{line_id}",
            json={"unit_price": 1.0},
        )
        assert response.status_code == 400
        assert "copied from the estimate" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_estimate_line_cannot_be_removed(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Dropping agreed work is a new invoice, not an edit to this one."""
        estimate = await create_approved_estimate(
            owner_client, test_customer, test_vehicle
        )
        ro = await create_billable_ro(
            owner_client, test_customer, test_vehicle, estimate["id"]
        )
        invoice = await create_invoice(owner_client, ro)
        line_id = invoice["items"][0]["id"]

        response = await owner_client.delete(
            f"{INVOICES_URL}{invoice['id']}/items/{line_id}"
        )
        assert response.status_code == 400
        assert "cannot be removed" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_line_on_an_unknown_invoice_is_not_found(
        self, owner_client: AsyncClient
    ):
        response = await owner_client.delete(
            f"{INVOICES_URL}00000000-0000-0000-0000-000000000000/items/"
            "00000000-0000-0000-0000-000000000001"
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_line_not_on_this_invoice_is_not_found(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(owner_client, ro)

        response = await owner_client.delete(
            f"{INVOICES_URL}{invoice['id']}/items/"
            "00000000-0000-0000-0000-000000000001"
        )
        assert response.status_code == 404


# --- header ----------------------------------------------------------------


class TestInvoiceHeader:
    @pytest.mark.asyncio
    async def test_update_dates_and_notes_on_a_draft(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(owner_client, ro)

        response = await owner_client.patch(
            f"{INVOICES_URL}{invoice['id']}",
            json={"notes": "Pay at the counter", "customer_notes": "Thank you"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["notes"] == "Pay at the counter"
        assert body["customer_notes"] == "Thank you"

    @pytest.mark.asyncio
    async def test_changing_the_tax_rate_recalculates(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=200.0)]
        )
        assert invoice["total"] == 200.0

        response = await owner_client.patch(
            f"{INVOICES_URL}{invoice['id']}", json={"tax_rate": 0.08}
        )
        body = response.json()
        assert body["tax_amount"] == 16.0
        assert body["total"] == 216.0

    @pytest.mark.asyncio
    async def test_delete_a_draft(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(owner_client, ro)

        response = await owner_client.delete(f"{INVOICES_URL}{invoice['id']}")
        assert response.status_code == 204

        gone = await owner_client.get(f"{INVOICES_URL}{invoice['id']}")
        assert gone.status_code == 404

    @pytest.mark.asyncio
    async def test_cannot_due_before_it_was_issued(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(owner_client, ro)

        response = await owner_client.patch(
            f"{INVOICES_URL}{invoice['id']}",
            json={"invoice_date": "2026-06-01", "due_date": "2026-05-01"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_the_service_also_refuses_impossible_dates(
        self, db: AsyncSession, owner_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """The service checks too, for any caller that reaches it without the
        API schema in front of it."""

        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(owner_client, ro)

        with pytest.raises(BusinessRuleError) as exc:
            await InvoiceService(db).update_invoice(
                invoice["id"],
                # model_construct skips the API schema's own date check, so the
                # service's backstop is what is under test here.
                InvoiceUpdate.model_construct(
                    invoice_date=date(2026, 6, 1), due_date=date(2026, 5, 1)
                ),
            )
        assert "due before it was issued" in str(exc.value)
        await db.rollback()


# --- lifecycle -------------------------------------------------------------


class TestInvoiceLifecycle:
    @pytest.mark.asyncio
    async def test_issue_a_draft(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=100.0)]
        )

        issued = await issue_invoice(owner_client, invoice["id"])
        assert issued["status"] == "ISSUED"
        assert issued["issued_at"] is not None

    @pytest.mark.asyncio
    async def test_an_issued_invoice_stops_being_editable(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """The customer holds the document, so its lines are now frozen too."""
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=100.0)]
        )
        await issue_invoice(owner_client, invoice["id"])

        add = await owner_client.post(
            f"{INVOICES_URL}{invoice['id']}/items", json=manual_line("Sneaky extra")
        )
        assert add.status_code == 400
        assert "only a DRAFT" in add.json()["detail"]

        edit = await owner_client.patch(
            f"{INVOICES_URL}{invoice['id']}", json={"tax_rate": 0.0}
        )
        assert edit.status_code == 400

    @pytest.mark.asyncio
    async def test_issued_invoice_cannot_be_deleted(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=100.0)]
        )
        await issue_invoice(owner_client, invoice["id"])

        response = await owner_client.delete(f"{INVOICES_URL}{invoice['id']}")
        assert response.status_code == 400
        assert "void it instead" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_cannot_issue_an_empty_invoice(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """There is nothing to charge."""
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(owner_client, ro)

        response = await owner_client.post(f"{INVOICES_URL}{invoice['id']}/issue")
        assert response.status_code == 400
        assert "no line items" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_cannot_issue_a_zero_value_invoice(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A discount that wipes the bill out leaves nothing to send."""
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=50.0)]
        )
        await owner_client.post(
            f"{INVOICES_URL}{invoice['id']}/items",
            json={
                "item_type": "DISCOUNT",
                "description": "Waived",
                "quantity": 1,
                "unit_price": 50.0,
            },
        )

        response = await owner_client.post(f"{INVOICES_URL}{invoice['id']}/issue")
        assert response.status_code == 400
        assert "nothing to charge" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_cannot_issue_twice(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=100.0)]
        )
        await issue_invoice(owner_client, invoice["id"])

        response = await owner_client.post(f"{INVOICES_URL}{invoice['id']}/issue")
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_void_an_issued_invoice(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=100.0)]
        )
        await issue_invoice(owner_client, invoice["id"])

        response = await owner_client.post(
            f"{INVOICES_URL}{invoice['id']}/void", params={"reason": "Billed in error"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "VOID"
        assert body["void_reason"] == "Billed in error"
        assert body["voided_at"] is not None

    @pytest.mark.asyncio
    async def test_void_requires_a_reason(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=100.0)]
        )
        await issue_invoice(owner_client, invoice["id"])

        response = await owner_client.post(
            f"{INVOICES_URL}{invoice['id']}/void", params={"reason": "  "}
        )
        assert response.status_code == 400
        assert "requires a reason" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_draft_is_deleted_rather_than_voided(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=100.0)]
        )

        response = await owner_client.post(
            f"{INVOICES_URL}{invoice['id']}/void", params={"reason": "Changed my mind"}
        )
        assert response.status_code == 400
        assert "delete it instead" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_voiding_twice_is_a_no_op(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=100.0)]
        )
        await issue_invoice(owner_client, invoice["id"])
        first = await owner_client.post(
            f"{INVOICES_URL}{invoice['id']}/void", params={"reason": "Wrong vehicle"}
        )
        second = await owner_client.post(
            f"{INVOICES_URL}{invoice['id']}/void", params={"reason": "Wrong vehicle"}
        )
        assert second.status_code == 200
        assert second.json()["void_reason"] == first.json()["void_reason"]

    @pytest.mark.asyncio
    async def test_there_is_no_generic_status_endpoint(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """An invoice may not declare itself paid: only money may do that."""
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=100.0)]
        )

        response = await owner_client.patch(
            f"{INVOICES_URL}{invoice['id']}/status", params={"status": "PAID"}
        )
        assert response.status_code == 404


# --- payment state ---------------------------------------------------------
#
# Payments are recorded through app.payments (Phase 15). These exercise the seam
# that module drives, so the invoice's paid state can only ever come from money.


class TestInvoicePaymentState:
    async def _issued_invoice(self, client, customer, vehicle, total=100.0) -> dict:
        ro = await create_billable_ro(client, customer, vehicle)
        invoice = await create_invoice(client, ro, extra_items=[manual_line(unit_price=total)])
        return await issue_invoice(client, invoice["id"])

    @pytest.mark.asyncio
    async def test_partial_payment(
        self, db: AsyncSession, owner_client: AsyncClient, test_customer, test_vehicle
    ):
        invoice = await self._issued_invoice(
            owner_client, test_customer, test_vehicle, 100.0
        )
        service = InvoiceService(db)
        loaded = await service.get_by_id(invoice["id"])
        await service.apply_payment(loaded, 40.0)
        await db.commit()

        refreshed = await service.get_by_id(invoice["id"])
        assert refreshed.status == InvoiceStatus.PARTIALLY_PAID.value
        assert float(refreshed.amount_paid) == 40.0
        assert refreshed.balance == 60.0

    @pytest.mark.asyncio
    async def test_payment_in_full_settles_the_invoice(
        self, db: AsyncSession, owner_client: AsyncClient, test_customer, test_vehicle
    ):
        invoice = await self._issued_invoice(
            owner_client, test_customer, test_vehicle, 100.0
        )
        service = InvoiceService(db)
        loaded = await service.get_by_id(invoice["id"])
        await service.apply_payment(loaded, 100.0)
        await db.commit()

        refreshed = await service.get_by_id(invoice["id"])
        assert refreshed.status == InvoiceStatus.PAID.value
        assert refreshed.balance == 0.0
        assert refreshed.paid_at is not None
        assert refreshed.has_balance is False

    @pytest.mark.asyncio
    async def test_two_payments_settle_the_invoice(
        self, db: AsyncSession, owner_client: AsyncClient, test_customer, test_vehicle
    ):
        invoice = await self._issued_invoice(
            owner_client, test_customer, test_vehicle, 100.0
        )
        service = InvoiceService(db)
        for amount in (30.0, 70.0):
            loaded = await service.get_by_id(invoice["id"])
            await service.apply_payment(loaded, amount)
            await db.commit()

        refreshed = await service.get_by_id(invoice["id"])
        assert refreshed.status == InvoiceStatus.PAID.value
        assert float(refreshed.amount_paid) == 100.0

    @pytest.mark.asyncio
    async def test_overpayment_is_refused(
        self, db: AsyncSession, owner_client: AsyncClient, test_customer, test_vehicle
    ):
        """An overpayment is a customer credit, not a negative invoice."""

        invoice = await self._issued_invoice(
            owner_client, test_customer, test_vehicle, 100.0
        )
        service = InvoiceService(db)
        loaded = await service.get_by_id(invoice["id"])

        with pytest.raises(BusinessRuleError) as exc:
            await service.apply_payment(loaded, 150.0)
        assert "exceeds" in str(exc.value)

    @pytest.mark.asyncio
    async def test_zero_payment_is_refused(
        self, db: AsyncSession, owner_client: AsyncClient, test_customer, test_vehicle
    ):

        invoice = await self._issued_invoice(
            owner_client, test_customer, test_vehicle, 100.0
        )
        service = InvoiceService(db)
        loaded = await service.get_by_id(invoice["id"])

        with pytest.raises(BusinessRuleError):
            await service.apply_payment(loaded, 0.0)

    @pytest.mark.asyncio
    async def test_a_draft_takes_no_payment(
        self, db: AsyncSession, owner_client: AsyncClient, test_customer, test_vehicle
    ):

        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=100.0)]
        )
        service = InvoiceService(db)
        loaded = await service.get_by_id(invoice["id"])

        with pytest.raises(BusinessRuleError) as exc:
            await service.apply_payment(loaded, 10.0)
        assert "only an ISSUED invoice" in str(exc.value)

    @pytest.mark.asyncio
    async def test_a_void_invoice_takes_no_payment(
        self, db: AsyncSession, owner_client: AsyncClient, test_customer, test_vehicle
    ):

        invoice = await self._issued_invoice(
            owner_client, test_customer, test_vehicle, 100.0
        )
        service = InvoiceService(db)
        await service.void_invoice(invoice["id"], "Billed in error")
        await db.commit()

        voided = await service.get_by_id(invoice["id"])
        with pytest.raises(BusinessRuleError) as exc:
            await service.apply_payment(voided, 10.0)
        assert "VOID" in str(exc.value)

    @pytest.mark.asyncio
    async def test_an_invoice_with_money_cannot_be_voided(
        self, db: AsyncSession, owner_client: AsyncClient, test_customer, test_vehicle
    ):
        """A payment is a fact; writing the bill off under it would hide cash."""

        invoice = await self._issued_invoice(
            owner_client, test_customer, test_vehicle, 100.0
        )
        service = InvoiceService(db)
        loaded = await service.get_by_id(invoice["id"])
        await service.apply_payment(loaded, 40.0)
        await db.commit()

        with pytest.raises(BusinessRuleError) as exc:
            await service.void_invoice(invoice["id"], "Changed my mind")
        assert "cannot be voided" in str(exc.value)

    @pytest.mark.asyncio
    async def test_a_paid_invoice_cannot_be_voided(
        self, db: AsyncSession, owner_client: AsyncClient, test_customer, test_vehicle
    ):

        invoice = await self._issued_invoice(
            owner_client, test_customer, test_vehicle, 100.0
        )
        service = InvoiceService(db)
        loaded = await service.get_by_id(invoice["id"])
        await service.apply_payment(loaded, 100.0)
        await db.commit()

        with pytest.raises(BusinessRuleError):
            await service.void_invoice(invoice["id"], "Settled in error")

    @pytest.mark.asyncio
    async def test_a_settled_invoice_takes_no_more_money(
        self, db: AsyncSession, owner_client: AsyncClient, test_customer, test_vehicle
    ):

        invoice = await self._issued_invoice(
            owner_client, test_customer, test_vehicle, 100.0
        )
        service = InvoiceService(db)
        loaded = await service.get_by_id(invoice["id"])
        await service.apply_payment(loaded, 100.0)
        await db.commit()

        settled = await service.get_by_id(invoice["id"])
        with pytest.raises(BusinessRuleError):
            await service.apply_payment(settled, 5.0)


# --- queries ---------------------------------------------------------------


class TestInvoiceQueries:
    @pytest.mark.asyncio
    async def test_list_invoices(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        await create_invoice(
            owner_client, await create_billable_ro(owner_client, test_customer, test_vehicle)
        )
        response = await owner_client.get(INVOICES_URL)
        assert response.status_code == 200
        assert response.json()["meta"]["total"] == 1

    @pytest.mark.asyncio
    async def test_filter_by_status(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=10.0)]
        )
        await issue_invoice(owner_client, invoice["id"])

        issued = await owner_client.get(f"{INVOICES_URL}?status=ISSUED")
        drafts = await owner_client.get(f"{INVOICES_URL}?status=DRAFT")
        assert issued.json()["meta"]["total"] == 1
        assert drafts.json()["meta"]["total"] == 0

    @pytest.mark.asyncio
    async def test_unknown_status_matches_nothing(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A mistyped filter must never silently return the whole list."""
        await create_invoice(
            owner_client, await create_billable_ro(owner_client, test_customer, test_vehicle)
        )
        response = await owner_client.get(f"{INVOICES_URL}?status=NOPE")
        assert response.json()["meta"]["total"] == 0

    @pytest.mark.asyncio
    async def test_filter_by_customer_and_repair_order(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(owner_client, ro)

        by_customer = await owner_client.get(
            f"{INVOICES_URL}?customer_id={test_customer.id}"
        )
        by_ro = await owner_client.get(
            f"{INVOICES_URL}?repair_order_id={ro['id']}"
        )
        assert by_customer.json()["meta"]["total"] == 1
        assert by_ro.json()["data"][0]["id"] == invoice["id"]

    @pytest.mark.asyncio
    async def test_filter_by_vehicle(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        await create_invoice(
            owner_client, await create_billable_ro(owner_client, test_customer, test_vehicle)
        )
        response = await owner_client.get(
            f"{INVOICES_URL}?vehicle_id={test_vehicle.id}"
        )
        assert response.json()["meta"]["total"] == 1

    @pytest.mark.asyncio
    async def test_search_by_invoice_number(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(owner_client, ro)

        hit = await owner_client.get(f"{INVOICES_URL}?search={invoice['invoice_number']}")
        miss = await owner_client.get(f"{INVOICES_URL}?search=INV-NOPE")
        assert hit.json()["meta"]["total"] == 1
        assert miss.json()["meta"]["total"] == 0

    @pytest.mark.asyncio
    async def test_filter_unpaid(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A draft has not been asked for yet, so it is not 'unpaid'."""
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        draft = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=10.0)]
        )
        await issue_invoice(owner_client, draft["id"])

        response = await owner_client.get(f"{INVOICES_URL}?unpaid_only=true")
        assert response.json()["meta"]["total"] == 1

        voided = await owner_client.post(
            f"{INVOICES_URL}{draft['id']}/void", params={"reason": "Wrong job"}
        )
        assert voided.status_code == 200
        after = await owner_client.get(f"{INVOICES_URL}?unpaid_only=true")
        assert after.json()["meta"]["total"] == 0

    @pytest.mark.asyncio
    async def test_filter_overdue(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Overdue is the clock and the balance, not a decision anybody made."""
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro,
            extra_items=[manual_line(unit_price=100.0)],
            invoice_date=(date.today() - timedelta(days=40)).isoformat(),
            due_date=(date.today() - timedelta(days=5)).isoformat(),
        )
        await issue_invoice(owner_client, invoice["id"])

        response = await owner_client.get(f"{INVOICES_URL}?overdue_only=true")
        assert response.json()["meta"]["total"] == 1
        body = response.json()["data"][0]
        assert body["is_overdue"] is True
        assert body["days_overdue"] == 5

    @pytest.mark.asyncio
    async def test_a_due_date_in_the_future_is_not_overdue(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro,
            extra_items=[manual_line(unit_price=100.0)],
            due_date=(date.today() + timedelta(days=5)).isoformat(),
        )
        await issue_invoice(owner_client, invoice["id"])

        response = await owner_client.get(f"{INVOICES_URL}?overdue_only=true")
        assert response.json()["meta"]["total"] == 0

    @pytest.mark.asyncio
    async def test_filter_by_date_range(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        await create_invoice(owner_client, ro)

        inside = await owner_client.get(
            f"{INVOICES_URL}?start_date=2000-01-01&end_date=2099-12-31"
        )
        outside = await owner_client.get(
            f"{INVOICES_URL}?start_date=2099-01-01&end_date=2099-12-31"
        )
        assert inside.json()["meta"]["total"] == 1
        assert outside.json()["meta"]["total"] == 0

    @pytest.mark.asyncio
    async def test_bad_date_filter_is_rejected(
        self, owner_client: AsyncClient
    ):
        response = await owner_client.get(f"{INVOICES_URL}?start_date=not-a-date")
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_summary(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=100.0)]
        )
        response = await owner_client.get(f"{INVOICES_URL}{invoice['id']}/summary")
        assert response.status_code == 200
        body = response.json()
        assert body["totals"]["total"] == 100.0
        assert body["totals"]["balance"] == 100.0
        assert body["item_count"] == 1

    @pytest.mark.asyncio
    async def test_get_unknown_invoice(
        self, owner_client: AsyncClient
    ):
        response = await owner_client.get(
            f"{INVOICES_URL}00000000-0000-0000-0000-000000000000"
        )
        assert response.status_code == 404


# --- integrity -------------------------------------------------------------


class TestInvoiceIntegrity:
    @pytest.mark.asyncio
    async def test_database_refuses_a_second_invoice_for_one_order(
        self, db: AsyncSession, owner_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """The one-invoice-per-job rule is enforced by the database, not only
        by the service, so a race cannot produce two bills for one repair."""
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(owner_client, ro)

        duplicate = Invoice(
            invoice_number="INV-DUPLICATE",
            customer_id=str(test_customer.id),
            vehicle_id=str(test_vehicle.id),
            repair_order_id=str(ro["id"]),
            invoice_date=date.today(),
            due_date=date.today(),
            tax_rate=0.0,
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()

        still_there = await db.get(Invoice, invoice["id"])
        assert still_there is not None

    @pytest.mark.asyncio
    async def test_database_refuses_an_unknown_invoice_status(
        self, db: AsyncSession, owner_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(owner_client, ro)

        with pytest.raises(IntegrityError):
            await db.execute(
                text("UPDATE invoices SET status = 'MAYBE' WHERE id = :id"),
                {"id": invoice["id"]},
            )
        await db.rollback()

    @pytest.mark.asyncio
    async def test_database_refuses_overpayment(
        self, db: AsyncSession, owner_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=100.0)]
        )
        await issue_invoice(owner_client, invoice["id"])

        with pytest.raises(IntegrityError):
            await db.execute(
                text("UPDATE invoices SET amount_paid = 500 WHERE id = :id"),
                {"id": invoice["id"]},
            )
        await db.rollback()

    @pytest.mark.asyncio
    async def test_database_refuses_a_negative_total(
        self, db: AsyncSession, owner_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(owner_client, ro)

        with pytest.raises(IntegrityError):
            await db.execute(
                text("UPDATE invoices SET total = -10 WHERE id = :id"),
                {"id": invoice["id"]},
            )
        await db.rollback()

    @pytest.mark.asyncio
    async def test_database_refuses_a_negative_discount(
        self, db: AsyncSession, owner_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(owner_client, ro)

        with pytest.raises(IntegrityError):
            await db.execute(
                text("UPDATE invoices SET discount_amount = -5 WHERE id = :id"),
                {"id": invoice["id"]},
            )
        await db.rollback()

    @pytest.mark.asyncio
    async def test_database_refuses_billing_a_line_twice(
        self, db: AsyncSession, owner_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """The same approval cannot be copied onto two invoices."""
        estimate = await create_approved_estimate(
            owner_client, test_customer, test_vehicle
        )
        ro = await create_billable_ro(
            owner_client, test_customer, test_vehicle, estimate["id"]
        )
        invoice = await create_invoice(owner_client, ro)
        first_item = invoice["items"][0]

        # A second, unrelated invoice for other work on the same vehicle.
        other_ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        other = await create_invoice(
            owner_client, other_ro, extra_items=[manual_line(unit_price=10.0)]
        )
        assert other["items"][0]["estimate_item_id"] is None

        # Hand the first invoice's approved line to the second one.
        db.add(
            InvoiceItem(
                invoice_id=other["id"],
                item_type="PART",
                source="ESTIMATE",
                description="Copied twice",
                quantity=1,
                unit_price=10.0,
                line_total=10.0,
                estimate_item_id=first_item["estimate_item_id"],
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()


# --- RBAC ------------------------------------------------------------------


class TestInvoiceRbac:
    @pytest.mark.asyncio
    async def test_customer_can_neither_read_nor_write_the_shop_invoice(
        self, owner_client: AsyncClient, customer_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """A customer may look at what they owe, but only through the portal.

        The shop's invoice endpoints are staff-only in both directions: the read
        is the shop's invoice book and the write is the shop's bill. The
        customer's own bills — the ones they are allowed to see, and to settle —
        are at ``/api/v1/portal/invoices``, where the invoice id is checked
        against the account the token belongs to.
        """
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=100.0)]
        )

        read = await customer_client.get(f"{INVOICES_URL}{invoice['id']}")
        assert read.status_code == 403

        listing = await customer_client.get(INVOICES_URL)
        assert listing.status_code == 403

        write = await customer_client.post(
            INVOICES_URL, json={"repair_order_id": ro["id"]}
        )
        assert write.status_code == 403

        line = await customer_client.post(
            f"{INVOICES_URL}{invoice['id']}/items", json=manual_line()
        )
        assert line.status_code == 403

    @pytest.mark.asyncio
    async def test_customer_cannot_issue_or_void(
        self, owner_client: AsyncClient, customer_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """Writing a bill off is a decision about money owed, not a document edit."""
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            owner_client, ro, extra_items=[manual_line(unit_price=100.0)]
        )
        await issue_invoice(owner_client, invoice["id"])

        issue = await customer_client.post(f"{INVOICES_URL}{invoice['id']}/issue")
        assert issue.status_code == 403

        void = await customer_client.post(
            f"{INVOICES_URL}{invoice['id']}/void", params={"reason": "Because"}
        )
        assert void.status_code == 403

    @pytest.mark.asyncio
    async def test_service_advisor_can_raise_and_issue(
        self, manager_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        ro = await create_billable_ro(manager_client, test_customer, test_vehicle)
        invoice = await create_invoice(
            manager_client, ro, extra_items=[manual_line(unit_price=100.0)]
        )
        issued = await issue_invoice(manager_client, invoice["id"])
        assert issued["status"] == "ISSUED"

    @pytest.mark.asyncio
    async def test_technician_is_denied(
        self, technician_client: AsyncClient
    ):
        response = await technician_client.get(INVOICES_URL)
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_parts_staff_is_denied(
        self, parts_client: AsyncClient
    ):
        response = await parts_client.get(INVOICES_URL)
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_unauthenticated_is_denied(
        self, unauth_client: AsyncClient
    ):
        response = await unauth_client.get(INVOICES_URL)
        assert response.status_code == 401
