# ruff: noqa: DTZ011
# The API returns the shop's local calendar date, so these assertions compare
# against the same local `date.today()`. Ruff would rather they used a UTC date,
# which would disagree with what the API returns.
"""Tests for payments.

Covers recording money against an invoice, partial settlement, the refusals that
protect the invoice's balance, voiding a payment and the balance it puts back,
payment queries, the shop-level takings summary, database-level integrity, and
RBAC.
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
from app.invoices.schemas import InvoiceUpdate  # noqa: F401  (used via model_construct)
from app.payments.services import PaymentService
from app.vehicles.models import Vehicle
from tests.factories import VehicleFactory

PAYMENTS_URL = "/api/v1/payments/"
INVOICES_URL = "/api/v1/invoices/"
ESTIMATES_URL = "/api/v1/estimates/"
RO_URL = "/api/v1/repair_orders/"


@pytest.fixture()
async def test_customer(db: AsyncSession):
    """A customer to hang the work off."""
    customer = Customer(
        first_name="Paying",
        last_name="Customer",
        email="payments_test@example.com",
        phone="555-0300",
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


async def _set_ro_status(client: AsyncClient, ro_id: str, status: str) -> dict:
    response = await client.patch(f"{RO_URL}{ro_id}/status", params={"status": status})
    assert response.status_code == 200, response.text
    return response.json()


async def create_billable_ro(
    client: AsyncClient, customer: Customer, vehicle: Vehicle
) -> dict:
    """Drive a repair order all the way to QC_PASSED, which is what can be billed."""
    created = await client.post(
        RO_URL,
        json={
            "customer_id": str(customer.id),
            "vehicle_id": str(vehicle.id),
            "tasks": [{"description": "Replace brake pads"}],
        },
    )
    assert created.status_code == 201, created.text
    ro = created.json()
    await _set_ro_status(client, ro["id"], "APPROVED")
    await _set_ro_status(client, ro["id"], "IN_PROGRESS")
    for task in ro["tasks"]:
        done = await client.patch(
            f"{RO_URL}{ro['id']}/tasks/{task['id']}/status",
            params={"status": "COMPLETED"},
        )
        assert done.status_code == 200, done.text
    await _set_ro_status(client, ro["id"], "COMPLETED")
    return await _set_ro_status(client, ro["id"], "QC_PASSED")


async def create_issued_invoice(
    client: AsyncClient,
    customer: Customer,
    vehicle: Vehicle,
    total: float = 100.0,
    **overrides,
) -> dict:
    """Raise and issue an invoice for ``total``, ready to take money."""
    ro = await create_billable_ro(client, customer, vehicle)
    created = await client.post(
        INVOICES_URL,
        json={
            "repair_order_id": ro["id"],
            "extra_items": [
                {
                    "item_type": "PART",
                    "description": "Brake pad set",
                    "quantity": 1,
                    "unit_price": total,
                }
            ],
            **overrides,
        },
    )
    assert created.status_code == 201, created.text
    invoice = created.json()
    issued = await client.post(f"{INVOICES_URL}{invoice['id']}/issue")
    assert issued.status_code == 200, issued.text
    return issued.json()


async def pay(client: AsyncClient, invoice_id: str, amount: float, **overrides) -> dict:
    """Record a payment against an invoice."""
    response = await client.post(
        PAYMENTS_URL, json={"invoice_id": invoice_id, "amount": amount, **overrides}
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- recording -------------------------------------------------------------


class TestRecordPayment:
    @pytest.mark.asyncio
    async def test_record_a_cash_payment(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Money taken and the invoice's balance move together."""
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 100.0)
        payment = await pay(owner_client, invoice["id"], 100.0, method="CASH")

        assert payment["amount"] == 100.0
        assert payment["method"] == "CASH"
        assert payment["status"] == "RECORDED"
        assert payment["invoice_id"] == invoice["id"]
        assert payment["recorded_by_id"] is not None

        refreshed = (await owner_client.get(f"{INVOICES_URL}{invoice['id']}")).json()
        assert refreshed["status"] == "PAID"
        assert refreshed["amount_paid"] == 100.0
        assert refreshed["balance"] == 0.0
        assert refreshed["paid_at"] is not None

    @pytest.mark.asyncio
    async def test_partial_payment_leaves_a_balance(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A bill can be settled over several visits."""
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 250.0)
        await pay(owner_client, invoice["id"], 100.0)

        refreshed = (await owner_client.get(f"{INVOICES_URL}{invoice['id']}")).json()
        assert refreshed["status"] == "PARTIALLY_PAID"
        assert refreshed["amount_paid"] == 100.0
        assert refreshed["balance"] == 150.0

    @pytest.mark.asyncio
    async def test_several_payments_settle_the_invoice(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 250.0)
        await pay(owner_client, invoice["id"], 100.0)
        await pay(owner_client, invoice["id"], 50.0)
        await pay(owner_client, invoice["id"], 100.0)

        refreshed = (await owner_client.get(f"{INVOICES_URL}{invoice['id']}")).json()
        assert refreshed["status"] == "PAID"
        assert refreshed["amount_paid"] == 250.0
        assert refreshed["balance"] == 0.0

    @pytest.mark.asyncio
    async def test_payment_defaults_to_today(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle)
        payment = await pay(owner_client, invoice["id"], 10.0)
        assert payment["payment_date"] == date.today().isoformat()

    @pytest.mark.asyncio
    async def test_backdated_payment(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A bill settled at the bank yesterday is dated yesterday."""
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle)
        when = (date.today() - timedelta(days=3)).isoformat()
        payment = await pay(owner_client, invoice["id"], 10.0, payment_date=when)
        assert payment["payment_date"] == when

    @pytest.mark.asyncio
    async def test_card_payment_keeps_its_reference(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle)
        payment = await pay(
            owner_client, invoice["id"], 100.0, method="CARD", reference="AUTH-99120"
        )
        assert payment["reference"] == "AUTH-99120"

    @pytest.mark.asyncio
    async def test_bank_transfer_requires_a_reference(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Money that leaves no paper trail cannot be reconciled at month end."""
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle)
        response = await owner_client.post(
            PAYMENTS_URL,
            json={"invoice_id": invoice["id"], "amount": 10.0, "method": "BANK_TRANSFER"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_cheque_requires_a_reference(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle)
        response = await owner_client.post(
            PAYMENTS_URL,
            json={
                "invoice_id": invoice["id"],
                "amount": 10.0,
                "method": "CHEQUE",
                "reference": "   ",
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_cash_needs_no_reference(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle)
        payment = await pay(owner_client, invoice["id"], 10.0, method="CASH")
        assert payment["reference"] is None

    @pytest.mark.asyncio
    async def test_unknown_method_is_rejected(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle)
        response = await owner_client.post(
            PAYMENTS_URL,
            json={"invoice_id": invoice["id"], "amount": 10.0, "method": "CRYPTO"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_zero_payment_is_rejected(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """There is no such thing as a payment of nothing."""
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle)
        response = await owner_client.post(
            PAYMENTS_URL, json={"invoice_id": invoice["id"], "amount": 0}
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_negative_payment_is_rejected(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A reversal is a void, not a negative payment."""
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle)
        response = await owner_client.post(
            PAYMENTS_URL, json={"invoice_id": invoice["id"], "amount": -50}
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_overpayment_is_refused(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """An overpayment is a customer credit, not a negative invoice."""
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 100.0)
        response = await owner_client.post(
            PAYMENTS_URL, json={"invoice_id": invoice["id"], "amount": 150.0}
        )
        assert response.status_code == 400
        assert "exceeds" in response.json()["detail"]

        listed = (await owner_client.get(f"{PAYMENTS_URL}?invoice_id={invoice['id']}")).json()
        assert listed["meta"]["total"] == 0

    @pytest.mark.asyncio
    async def test_payment_against_a_draft_is_refused(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A bill that has not been sent cannot be paid."""
        ro = await create_billable_ro(owner_client, test_customer, test_vehicle)
        created = await owner_client.post(
            INVOICES_URL,
            json={
                "repair_order_id": ro["id"],
                "extra_items": [
                    {"item_type": "PART", "description": "Pad set", "quantity": 1, "unit_price": 50.0}
                ],
            },
        )
        invoice = created.json()

        response = await owner_client.post(
            PAYMENTS_URL, json={"invoice_id": invoice["id"], "amount": 10.0}
        )
        assert response.status_code == 400
        assert "only an ISSUED invoice" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_payment_against_a_void_invoice_is_refused(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 100.0)
        voided = await owner_client.post(
            f"{INVOICES_URL}{invoice['id']}/void", params={"reason": "Wrong job"}
        )
        assert voided.status_code == 200

        response = await owner_client.post(
            PAYMENTS_URL, json={"invoice_id": invoice["id"], "amount": 10.0}
        )
        assert response.status_code == 400
        assert "VOID" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_payment_against_unknown_invoice(
        self, owner_client: AsyncClient
    ):
        response = await owner_client.post(
            PAYMENTS_URL,
            json={
                "invoice_id": "00000000-0000-0000-0000-000000000000",
                "amount": 10.0,
            },
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_a_settled_invoice_takes_no_more(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 100.0)
        await pay(owner_client, invoice["id"], 100.0)

        response = await owner_client.post(
            PAYMENTS_URL, json={"invoice_id": invoice["id"], "amount": 5.0}
        )
        assert response.status_code == 400


# --- voiding ---------------------------------------------------------------


class TestVoidPayment:
    @pytest.mark.asyncio
    async def test_void_puts_the_money_back(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """The row survives as the explanation; the balance comes back."""
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 100.0)
        payment = await pay(owner_client, invoice["id"], 100.0)

        response = await owner_client.post(
            f"{PAYMENTS_URL}{payment['id']}/void", json={"reason": "Wrong card"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "VOID"
        assert body["void_reason"] == "Wrong card"
        assert body["voided_at"] is not None

        refreshed = (await owner_client.get(f"{INVOICES_URL}{invoice['id']}")).json()
        assert refreshed["status"] == "ISSUED"
        assert refreshed["amount_paid"] == 0.0
        assert refreshed["balance"] == 100.0
        assert refreshed["paid_at"] is None

    @pytest.mark.asyncio
    async def test_voiding_one_of_two_payments(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 200.0)
        first = await pay(owner_client, invoice["id"], 50.0)
        await pay(owner_client, invoice["id"], 50.0)

        await owner_client.post(
            f"{PAYMENTS_URL}{first['id']}/void", json={"reason": "Keyed twice"}
        )
        refreshed = (await owner_client.get(f"{INVOICES_URL}{invoice['id']}")).json()
        assert refreshed["status"] == "PARTIALLY_PAID"
        assert refreshed["amount_paid"] == 50.0
        assert refreshed["balance"] == 150.0

    @pytest.mark.asyncio
    async def test_voiding_twice_is_a_no_op(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A double-click must not reverse the same money twice."""
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 100.0)
        payment = await pay(owner_client, invoice["id"], 100.0)
        await owner_client.post(
            f"{PAYMENTS_URL}{payment['id']}/void", json={"reason": "Wrong card"}
        )

        second = await owner_client.post(
            f"{PAYMENTS_URL}{payment['id']}/void", json={"reason": "Wrong card"}
        )
        assert second.status_code == 200
        assert second.json()["status"] == "VOID"

        refreshed = (await owner_client.get(f"{INVOICES_URL}{invoice['id']}")).json()
        assert refreshed["amount_paid"] == 0.0
        assert refreshed["balance"] == 100.0

    @pytest.mark.asyncio
    async def test_void_requires_a_reason(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 100.0)
        payment = await pay(owner_client, invoice["id"], 100.0)

        response = await owner_client.post(
            f"{PAYMENTS_URL}{payment['id']}/void", json={"reason": ""}
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_service_refuses_a_blank_reason(
        self, db: AsyncSession, owner_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """The service checks too, for callers that never pass the API schema."""
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 100.0)
        payment = await pay(owner_client, invoice["id"], 100.0)

        with pytest.raises(BusinessRuleError) as exc:
            await PaymentService(db).void_payment(payment["id"], "   ")
        assert "requires a reason" in str(exc.value)
        await db.rollback()

    @pytest.mark.asyncio
    async def test_payments_cannot_be_deleted_or_edited(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A record of what a customer handed over is a fact, not a draft."""
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 100.0)
        payment = await pay(owner_client, invoice["id"], 50.0)

        assert (await owner_client.delete(f"{PAYMENTS_URL}{payment['id']}")).status_code == 405
        assert (
            await owner_client.patch(
                f"{PAYMENTS_URL}{payment['id']}", json={"amount": 1.0}
            )
        ).status_code == 405

    @pytest.mark.asyncio
    async def test_an_invoice_with_money_now_be_voidable_after_the_reversal(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Voiding the payment is what makes writing the bill off possible."""
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 100.0)
        payment = await pay(owner_client, invoice["id"], 100.0)

        blocked = await owner_client.post(
            f"{INVOICES_URL}{invoice['id']}/void", params={"reason": "Wrong job"}
        )
        assert blocked.status_code == 400

        await owner_client.post(
            f"{PAYMENTS_URL}{payment['id']}/void", json={"reason": "Taken in error"}
        )
        allowed = await owner_client.post(
            f"{INVOICES_URL}{invoice['id']}/void", params={"reason": "Wrong job"}
        )
        assert allowed.status_code == 200
        assert allowed.json()["status"] == "VOID"


# --- queries ---------------------------------------------------------------


class TestPaymentQueries:
    @pytest.mark.asyncio
    async def test_list_payments(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 200.0)
        await pay(owner_client, invoice["id"], 50.0)
        await pay(owner_client, invoice["id"], 50.0)

        response = await owner_client.get(PAYMENTS_URL)
        assert response.status_code == 200
        assert response.json()["meta"]["total"] == 2

    @pytest.mark.asyncio
    async def test_filter_by_invoice(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 200.0)
        await pay(owner_client, invoice["id"], 50.0)

        mine = await owner_client.get(f"{PAYMENTS_URL}?invoice_id={invoice['id']}")
        other = await owner_client.get(
            f"{PAYMENTS_URL}?invoice_id=00000000-0000-0000-0000-000000000000"
        )
        assert mine.json()["meta"]["total"] == 1
        assert other.json()["meta"]["total"] == 0

    @pytest.mark.asyncio
    async def test_filter_by_customer(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """'What has this customer paid us' reaches through the invoice."""
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 200.0)
        await pay(owner_client, invoice["id"], 50.0)

        response = await owner_client.get(
            f"{PAYMENTS_URL}?customer_id={test_customer.id}"
        )
        assert response.json()["meta"]["total"] == 1

    @pytest.mark.asyncio
    async def test_filter_by_method_and_status(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 300.0)
        cash = await pay(owner_client, invoice["id"], 50.0, method="CASH")
        await pay(
            owner_client, invoice["id"], 50.0, method="CARD", reference="AUTH-1"
        )
        await owner_client.post(
            f"{PAYMENTS_URL}{cash['id']}/void", json={"reason": "Keyed twice"}
        )

        card = await owner_client.get(f"{PAYMENTS_URL}?method=CARD")
        voided = await owner_client.get(f"{PAYMENTS_URL}?status=VOID")
        recorded = await owner_client.get(f"{PAYMENTS_URL}?status=RECORDED")
        assert card.json()["meta"]["total"] == 1
        assert voided.json()["meta"]["total"] == 1
        assert recorded.json()["meta"]["total"] == 1

    @pytest.mark.asyncio
    async def test_unknown_filters_match_nothing(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A mistyped filter must never silently return the whole list."""
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 200.0)
        await pay(owner_client, invoice["id"], 50.0)

        assert (await owner_client.get(f"{PAYMENTS_URL}?method=GOLD")).json()["meta"]["total"] == 0
        assert (
            await owner_client.get(f"{PAYMENTS_URL}?status=REFUNDED")
        ).json()["meta"]["total"] == 0

    @pytest.mark.asyncio
    async def test_search_by_reference(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 300.0)
        await pay(owner_client, invoice["id"], 50.0, method="CARD", reference="AUTH-77881")

        hit = await owner_client.get(f"{PAYMENTS_URL}?search=AUTH-77881")
        miss = await owner_client.get(f"{PAYMENTS_URL}?search=AUTH-00000")
        assert hit.json()["meta"]["total"] == 1
        assert miss.json()["meta"]["total"] == 0

    @pytest.mark.asyncio
    async def test_filter_by_date_range(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 200.0)
        await pay(
            owner_client, invoice["id"], 50.0,
            payment_date=(date.today() - timedelta(days=10)).isoformat(),
        )

        inside = await owner_client.get(
            f"{PAYMENTS_URL}?start_date=2000-01-01&end_date=2099-12-31"
        )
        outside = await owner_client.get(
            f"{PAYMENTS_URL}?start_date=2099-01-01&end_date=2099-12-31"
        )
        assert inside.json()["meta"]["total"] == 1
        assert outside.json()["meta"]["total"] == 0

    @pytest.mark.asyncio
    async def test_get_a_single_payment(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 200.0)
        payment = await pay(owner_client, invoice["id"], 50.0)

        response = await owner_client.get(f"{PAYMENTS_URL}{payment['id']}")
        assert response.status_code == 200
        assert response.json()["id"] == payment["id"]

    @pytest.mark.asyncio
    async def test_get_unknown_payment(
        self, owner_client: AsyncClient
    ):
        response = await owner_client.get(
            f"{PAYMENTS_URL}00000000-0000-0000-0000-000000000000"
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_summary_counts_money_in(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 300.0)
        await pay(owner_client, invoice["id"], 100.0, method="CASH")
        await pay(owner_client, invoice["id"], 50.0, method="CARD", reference="AUTH-2")

        response = await owner_client.get(f"{PAYMENTS_URL}summary")
        assert response.status_code == 200
        totals = response.json()["totals"]
        assert totals["total_received"] == 150.0
        assert totals["net_received"] == 150.0
        assert totals["payment_count"] == 2
        assert totals["void_count"] == 0
        assert totals["by_method"] == {"CASH": 100.0, "CARD": 50.0}

    @pytest.mark.asyncio
    async def test_summary_subtracts_reversals(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A bad afternoon at the till shows as what it was, not as takings."""
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 300.0)
        good = await pay(owner_client, invoice["id"], 100.0, method="CASH")
        await pay(owner_client, invoice["id"], 50.0, method="CARD", reference="AUTH-3")
        await owner_client.post(
            f"{PAYMENTS_URL}{good['id']}/void", json={"reason": "Keyed twice"}
        )

        totals = (await owner_client.get(f"{PAYMENTS_URL}summary")).json()["totals"]
        assert totals["total_received"] == 50.0
        assert totals["total_voided"] == 100.0
        assert totals["net_received"] == -50.0
        assert totals["void_count"] == 1

    @pytest.mark.asyncio
    async def test_summary_over_a_date_range(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 300.0)
        await pay(owner_client, invoice["id"], 100.0)
        await pay(
            owner_client, invoice["id"], 50.0,
            payment_date=(date.today() - timedelta(days=40)).isoformat(),
        )

        today_only = await owner_client.get(
            f"{PAYMENTS_URL}summary?start_date={date.today()}&end_date={date.today()}"
        )
        assert today_only.json()["totals"]["total_received"] == 100.0

        last_year = await owner_client.get(
            f"{PAYMENTS_URL}summary?start_date=2000-01-01&end_date=2000-12-31"
        )
        assert last_year.json()["totals"]["total_received"] == 0.0

    @pytest.mark.asyncio
    async def test_list_for_invoice(
        self, db: AsyncSession, owner_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 300.0)
        await pay(owner_client, invoice["id"], 50.0)
        await pay(owner_client, invoice["id"], 50.0)

        payments = await PaymentService(db).list_for_invoice(invoice["id"])
        assert len(payments) == 2
        assert all(str(p.invoice_id) == invoice["id"] for p in payments)


# --- integrity -------------------------------------------------------------


class TestPaymentIntegrity:
    @pytest.mark.asyncio
    async def test_database_refuses_a_zero_payment(
        self, db: AsyncSession, owner_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 100.0)
        await pay(owner_client, invoice["id"], 100.0)

        with pytest.raises(IntegrityError):
            await db.execute(
                text("UPDATE payments SET amount = 0 WHERE invoice_id = :id"),
                {"id": invoice["id"]},
            )
        await db.rollback()

    @pytest.mark.asyncio
    async def test_database_refuses_a_negative_payment(
        self, db: AsyncSession, owner_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 100.0)
        await pay(owner_client, invoice["id"], 100.0)

        with pytest.raises(IntegrityError):
            await db.execute(
                text("UPDATE payments SET amount = -20 WHERE invoice_id = :id"),
                {"id": invoice["id"]},
            )
        await db.rollback()

    @pytest.mark.asyncio
    async def test_database_refuses_an_unknown_method(
        self, db: AsyncSession, owner_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 100.0)
        await pay(owner_client, invoice["id"], 100.0)

        with pytest.raises(IntegrityError):
            await db.execute(
                text("UPDATE payments SET method = 'CRYPTO' WHERE invoice_id = :id"),
                {"id": invoice["id"]},
            )
        await db.rollback()

    @pytest.mark.asyncio
    async def test_database_refuses_an_unknown_status(
        self, db: AsyncSession, owner_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 100.0)
        await pay(owner_client, invoice["id"], 100.0)

        with pytest.raises(IntegrityError):
            await db.execute(
                text("UPDATE payments SET status = 'REFUNDED' WHERE invoice_id = :id"),
                {"id": invoice["id"]},
            )
        await db.rollback()

    @pytest.mark.asyncio
    async def test_a_payment_cannot_outlive_its_invoice(
        self, db: AsyncSession, owner_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """The invoice holds money taken; deleting it out from under a payment
        would leave cash received against nothing."""
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 100.0)
        await pay(owner_client, invoice["id"], 100.0)

        response = await owner_client.delete(f"{INVOICES_URL}{invoice['id']}")
        assert response.status_code == 400
        assert "void it instead" in response.json()["detail"]


# --- RBAC ------------------------------------------------------------------


class TestPaymentRbac:
    @pytest.mark.asyncio
    async def test_customer_pays_their_own_bill_through_the_portal_not_the_till(
        self, owner_client: AsyncClient, customer_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """Paying is something a customer does — through the portal, not the till.

        ``payments:write`` is the customer's permission, so before the staff
        boundary this endpoint let them record a payment against *any* invoice in
        the shop, including somebody else's. That is the whole reason the till is
        staff-only and the portal is not: the same permission, two very different
        scopes. The portal's own payment tests live in ``test_portal.py``.
        """
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 100.0)
        response = await customer_client.post(
            PAYMENTS_URL, json={"invoice_id": invoice["id"], "amount": 100.0}
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_customer_cannot_refund(
        self, owner_client: AsyncClient, customer_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """A payer may settle a bill; only the shop decides what leaves the till."""
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 100.0)
        payment = await pay(owner_client, invoice["id"], 100.0)

        response = await customer_client.post(
            f"{PAYMENTS_URL}{payment['id']}/void", json={"reason": "Changed my mind"}
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_customer_cannot_read_the_till(
        self, owner_client: AsyncClient, customer_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """`GET /payments/` is every payment in the business, not the caller's own."""
        invoice = await create_issued_invoice(owner_client, test_customer, test_vehicle, 100.0)
        await pay(owner_client, invoice["id"], 100.0)

        assert (await customer_client.get(PAYMENTS_URL)).status_code == 403
        assert (await customer_client.get(f"{PAYMENTS_URL}summary")).status_code == 403

    @pytest.mark.asyncio
    async def test_technician_is_denied(
        self, technician_client: AsyncClient
    ):
        assert (await technician_client.get(PAYMENTS_URL)).status_code == 403

    @pytest.mark.asyncio
    async def test_parts_staff_is_denied(
        self, parts_client: AsyncClient
    ):
        assert (await parts_client.get(PAYMENTS_URL)).status_code == 403

    @pytest.mark.asyncio
    async def test_service_advisor_runs_the_counter(
        self, manager_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Taking money and reversing a mistake are both counter work."""
        invoice = await create_issued_invoice(
            manager_client, test_customer, test_vehicle, 100.0
        )
        recorded = await manager_client.post(
            PAYMENTS_URL, json={"invoice_id": invoice["id"], "amount": 50.0}
        )
        assert recorded.status_code == 201

        refunded = await manager_client.post(
            f"{PAYMENTS_URL}{recorded.json()['id']}/void", json={"reason": "Refund"}
        )
        assert refunded.status_code == 200
        assert refunded.json()["status"] == "VOID"

    @pytest.mark.asyncio
    async def test_unauthenticated_is_denied(
        self, unauth_client: AsyncClient
    ):
        assert (await unauth_client.get(PAYMENTS_URL)).status_code == 401
