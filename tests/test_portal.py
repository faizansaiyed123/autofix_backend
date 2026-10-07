"""Tests for the customer portal.

The portal has one job that matters more than anything else in it: a customer
sees their own data and nothing else. Most of these tests exist to pin that down,
because a portal that leaks one row of somebody else's account is a different
kind of bug from one that renders a label wrong — it is a privacy incident, and
it is silent.

Two properties are checked deliberately and repeatedly:

* **Ownership.** A record belonging to another customer is reported as *not
  found*, not as forbidden. A 403 would confirm the id exists, which is itself
  the leak. Several tests assert 404 specifically for this reason.
* **Translation.** The shop's status codes are not customer-facing. An invoice in
  ``PARTIALLY_PAID`` reads as "Part paid" with a balance. The raw ``status`` is
  still present — it is what the shop's own screens key off — so the tests assert
  the code is *translated alongside*, not merely replaced.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.appointments.models import Appointment
from app.auth.models import Role, User, UserRole
from app.auth.permissions import RoleEnum
from app.auth.services import AuthService
from app.core.database import get_session
from app.core.security import hash_password
from app.customers.models import Customer
from app.estimates.models import (
    Estimate,
    EstimateItem,
    EstimateItemStatus,
    EstimateItemType,
    EstimateStatus,
)
from app.invoices.models import Invoice, InvoiceItem, InvoiceStatus
from app.main import app
from app.payments.models import Payment, PaymentStatus
from app.portal.statuses import describe
from app.repair_orders.models import RepairOrder, RepairOrderStatus
from app.service_requests.models import ServiceRequest
from tests.factories import CustomerFactory, VehicleFactory

PORTAL = "/api/v1/portal"


def _today() -> date:
    return date.today()  # noqa: DTZ011


# --- helpers -----------------------------------------------------------------


async def _create_portal_customer(db: AsyncSession, label: str) -> Customer:
    """A customer record with a real CUSTOMER-role login behind it.

    The seeded demo customer is a bare ``User``: no ``Customer`` row, so there is
    no portal to see. Every test that needs a portal needs this pair.
    """
    user = User(
        email=f"{label.lower()}-{uuid.uuid4().hex[:8]}@test.com",
        password_hash=hash_password("demo1234"),
        first_name=label,
        last_name="Tester",
        is_active=True,
        # Customers are not staff. The portal still works for them; the shop's
        # back-office endpoints still refuse them.
        is_staff=False,
    )
    db.add(user)
    await db.flush()

    role = (
        await db.execute(select(Role).where(Role.name == RoleEnum.CUSTOMER.value))
    ).scalar_one()
    db.add(UserRole(user_id=user.id, role_id=role.id))

    customer = CustomerFactory.build(user_id=user.id)
    db.add(customer)
    await db.commit()
    return customer


async def _client_for(db: AsyncSession, customer: Customer) -> AsyncClient:
    """An AsyncClient signed in as the user behind ``customer``."""
    user = (
        await db.execute(select(User).where(User.id == customer.user_id))
    ).scalar_one()
    token, _ = AuthService(db).generate_tokens(user)

    async def _override():
        yield db

    app.dependency_overrides[get_session] = _override
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    )


def _add_vehicle(db: AsyncSession, customer: Customer):
    vehicle = VehicleFactory.build(customer_id=customer.id)
    db.add(vehicle)
    return vehicle


async def _add_estimate(
    db: AsyncSession,
    customer: Customer,
    vehicle,
    *,
    number: str | None = None,
    status: str = EstimateStatus.SENT.value,
    valid_until: date | None = None,
    item_status: str = EstimateItemStatus.PENDING.value,
    description: str = "Brake pads, front axle",
    line_total: float = 320.00,
) -> Estimate:
    # The item is attached through the relationship *before* the parent is
    # flushed. Appending to a collection on an already-persistent object makes
    # SQLAlchemy load the collection first, which is a lazy load outside the
    # greenlet — so the graph is built while it is still transient.
    estimate = Estimate(
        estimate_number=number or f"EST-{uuid.uuid4().hex[:8].upper()}",
        customer_id=customer.id,
        vehicle_id=vehicle.id,
        status=status,
        valid_until=valid_until,
        subtotal=line_total,
        tax_rate=0.0,
        tax_amount=0.0,
        total=line_total,
        items=[
            EstimateItem(
                item_type=EstimateItemType.PART.value,
                description=description,
                sequence=1,
                quantity=1.0,
                unit_price=line_total,
                line_total=line_total,
                status=item_status,
            )
        ],
    )
    db.add(estimate)
    await db.commit()
    return estimate


async def _add_estimate_line(
    db: AsyncSession,
    estimate: Estimate,
    *,
    description: str,
    line_total: float,
    sequence: int = 2,
) -> EstimateItem:
    item = EstimateItem(
        estimate_id=estimate.id,
        item_type=EstimateItemType.LABOR.value,
        description=description,
        sequence=sequence,
        quantity=1.0,
        unit_price=line_total,
        line_total=line_total,
        status=EstimateItemStatus.PENDING.value,
    )
    db.add(item)
    await db.commit()
    return item


async def _add_invoice(
    db: AsyncSession,
    customer: Customer,
    vehicle,
    *,
    number: str | None = None,
    status: str = InvoiceStatus.ISSUED.value,
    total: float = 500.00,
    amount_paid: float = 0.0,
    due_date: date | None = None,
) -> Invoice:
    """An invoice, with the repair order its schema requires behind it."""
    ro = RepairOrder(
        ro_number=f"RO-{uuid.uuid4().hex[:8].upper()}",
        customer_id=customer.id,
        vehicle_id=vehicle.id,
        status=RepairOrderStatus.QC_PASSED.value,
    )
    db.add(ro)
    await db.flush()

    invoice = Invoice(
        invoice_number=number or f"INV-{uuid.uuid4().hex[:8].upper()}",
        customer_id=customer.id,
        vehicle_id=vehicle.id,
        repair_order_id=ro.id,
        status=status,
        invoice_date=_today(),
        due_date=due_date,
        subtotal=total,
        tax_amount=0.0,
        total=total,
        amount_paid=amount_paid,
        items=[
            InvoiceItem(
                item_type="LABOR",
                source="MANUAL",
                description="Diagnostic and repair labour",
                sequence=1,
                quantity=1.0,
                unit_price=total,
                line_total=total,
            )
        ],
    )
    db.add(invoice)
    await db.commit()
    return invoice


async def _add_payment(
    db: AsyncSession, invoice: Invoice, amount: float
) -> Payment:
    payment = Payment(
        invoice_id=invoice.id,
        amount=Decimal(str(amount)),
        method="CARD",
        status=PaymentStatus.RECORDED.value,
        payment_date=_today(),
    )
    db.add(payment)
    await db.commit()
    return payment


async def _add_appointment(
    db: AsyncSession,
    customer: Customer,
    vehicle,
    *,
    days_ahead: int = 3,
    status: str = "CONFIRMED",
) -> Appointment:
    appointment = Appointment(
        customer_id=customer.id,
        vehicle_id=vehicle.id,
        service_type="Brake inspection",
        scheduled_start=datetime.now(UTC) + timedelta(days=days_ahead),
        status=status,
    )
    db.add(appointment)
    await db.commit()
    return appointment


# --- fixtures ----------------------------------------------------------------


@pytest.fixture()
async def mine(db: AsyncSession):
    """The signed-in customer, their one vehicle, and a client bound to them."""
    customer = await _create_portal_customer(db, "Alice")
    vehicle = _add_vehicle(db, customer)
    await db.commit()
    client = await _client_for(db, customer)
    yield customer, vehicle, client
    await client.aclose()
    app.dependency_overrides.clear()


@pytest.fixture()
async def theirs(db: AsyncSession):
    """A second, unrelated customer — the "somebody else" in every isolation test."""
    customer = await _create_portal_customer(db, "Bob")
    vehicle = _add_vehicle(db, customer)
    await db.commit()
    client = await _client_for(db, customer)
    yield customer, vehicle, client
    await client.aclose()
    app.dependency_overrides.clear()


# --- access control ----------------------------------------------------------


class TestPortalAccess:
    async def test_unauthenticated_is_refused(self, unauth_client):
        response = await unauth_client.get(f"{PORTAL}/")
        assert response.status_code == 401

    async def test_every_endpoint_requires_auth(self, unauth_client):
        for path in (
            "/",
            "/vehicles",
            "/appointments",
            "/service-requests",
            "/estimates",
            "/invoices",
            "/payments",
        ):
            response = await unauth_client.get(f"{PORTAL}{path}")
            assert response.status_code == 401, path

    async def test_staff_without_a_customer_record_gets_404(self, owner_client):
        """The owner is fully permitted but has no customer record.

        404, not 403: there is nothing behind the door, so there is nothing they
        are forbidden from seeing.
        """
        response = await owner_client.get(f"{PORTAL}/")
        assert response.status_code == 404
        assert "customer record" in response.json()["detail"].lower()

    async def test_customer_can_reach_the_portal(self, mine):
        _, _, client = mine
        response = await client.get(f"{PORTAL}/")
        assert response.status_code == 200


# --- the ownership rule ------------------------------------------------------


class TestPortalOwnership:
    async def test_vehicles_are_scoped_to_the_signed_in_customer(self, mine, theirs):
        _, _, my_client = mine
        _, _, their_client = theirs

        my_ids = {v["id"] for v in (await my_client.get(f"{PORTAL}/vehicles")).json()}
        their_ids = {
            v["id"] for v in (await their_client.get(f"{PORTAL}/vehicles")).json()
        }

        assert len(my_ids) == 1
        assert len(their_ids) == 1
        assert not my_ids & their_ids

    async def test_another_customers_vehicle_is_not_found(self, mine, theirs):
        _, _, client = mine
        _, their_vehicle, _ = theirs

        response = await client.get(f"{PORTAL}/vehicles/{their_vehicle.id}")
        # 404, not 403: a 403 would confirm the id exists.
        assert response.status_code == 404

    async def test_another_customers_invoice_is_not_found(self, mine, theirs, db):
        _, _, client = mine
        their_customer, their_vehicle, _ = theirs
        invoice = await _add_invoice(db, their_customer, their_vehicle)

        response = await client.get(f"{PORTAL}/invoices/{invoice.id}")
        assert response.status_code == 404

    async def test_another_customers_estimate_is_not_found(self, mine, theirs, db):
        _, _, client = mine
        their_customer, their_vehicle, _ = theirs
        estimate = await _add_estimate(db, their_customer, their_vehicle)

        response = await client.get(f"{PORTAL}/estimates/{estimate.id}")
        assert response.status_code == 404

    async def test_invoice_list_excludes_other_customers(self, mine, theirs, db):
        customer, vehicle, client = mine
        their_customer, their_vehicle, _ = theirs
        their_invoice = await _add_invoice(db, their_customer, their_vehicle)
        await _add_invoice(db, customer, vehicle, number="INV-MINE")

        listed = (await client.get(f"{PORTAL}/invoices")).json()

        assert [i["invoice_number"] for i in listed] == ["INV-MINE"]
        assert their_invoice.id not in {i["id"] for i in listed}

    async def test_payments_are_scoped_through_the_invoice(self, mine, theirs, db):
        """A payment is scoped by joining to its invoice, not by filtering after."""
        _, _, client = mine
        their_customer, their_vehicle, _ = theirs
        their_invoice = await _add_invoice(db, their_customer, their_vehicle, total=999.0)
        await _add_payment(db, their_invoice, 999.0)

        assert (await client.get(f"{PORTAL}/payments")).json() == []

    async def test_own_payments_are_listed(self, mine, db):
        customer, vehicle, client = mine
        invoice = await _add_invoice(db, customer, vehicle, total=120.0)
        await _add_payment(db, invoice, 120.0)

        listed = (await client.get(f"{PORTAL}/payments")).json()

        assert len(listed) == 1
        assert listed[0]["amount"] == 120.0
        assert listed[0]["invoice_number"] == invoice.invoice_number

    async def test_service_requests_are_scoped(self, mine, theirs, db):
        from app.service_requests.models import ServiceRequest

        _, _, client = mine
        their_customer, their_vehicle, _ = theirs
        db.add(
            ServiceRequest(
                customer_id=their_customer.id,
                vehicle_id=their_vehicle.id,
                title="Their brakes grind",
            )
        )
        await db.commit()

        assert (await client.get(f"{PORTAL}/service-requests")).json() == []


# --- status translation ------------------------------------------------------


class TestStatusTranslation:
    async def test_estimate_status_is_translated_not_leaked(self, mine, db):
        customer, vehicle, client = mine
        await _add_estimate(db, customer, vehicle)

        body = (await client.get(f"{PORTAL}/estimates")).json()[0]
        view = body["status_view"]

        assert body["status"] == EstimateStatus.SENT.value
        assert view["status"] == EstimateStatus.SENT.value
        assert view["label"]
        # The shop's screaming-code vocabulary is not what is shown to a customer.
        assert view["label"] != body["status"]
        assert "_" not in view["label"]
        assert view["detail"]

    async def test_invoice_status_is_translated(self, mine, db):
        customer, vehicle, client = mine
        await _add_invoice(
            db, customer, vehicle,
            status=InvoiceStatus.PARTIALLY_PAID.value, amount_paid=200.0,
        )

        body = (await client.get(f"{PORTAL}/invoices")).json()[0]

        assert body["status"] == InvoiceStatus.PARTIALLY_PAID.value
        assert body["status_view"]["label"] == "Part paid"
        assert body["status_view"]["status"] == InvoiceStatus.PARTIALLY_PAID.value

    def test_unknown_status_falls_back_to_readable_text(self):
        """A status the translation table has not caught up with.

        Better a title-cased fallback than a raw screaming code in a customer
        document — the customer still has to see something.
        """
        view = describe("SOME_NEW_THING", {})
        assert view.status == "SOME_NEW_THING"
        assert view.label == "Some New Thing"

    async def test_appointment_status_is_translated(self, mine, db):
        customer, vehicle, client = mine
        await _add_appointment(db, customer, vehicle)

        body = (await client.get(f"{PORTAL}/appointments")).json()[0]

        assert body["status"] == "CONFIRMED"
        assert body["status_view"]["label"] == "Confirmed"
        assert body["vehicle_label"], "the appointment should name the vehicle"

    async def test_vehicle_status_uses_customer_words(self, mine):
        _, _, client = mine

        body = (await client.get(f"{PORTAL}/vehicles")).json()[0]

        assert body["status"] == "ACTIVE"
        assert body["status_view"]["label"] == "With you"

    async def test_in_shop_vehicle_reads_as_in_the_shop(self, mine, db):
        _, vehicle, client = mine
        vehicle.status = "IN_SHOP"
        await db.commit()

        body = (await client.get(f"{PORTAL}/vehicles")).json()[0]
        assert body["status_view"]["label"] == "In the shop"


# --- money -------------------------------------------------------------------


class TestPortalMoney:
    async def test_invoice_reports_the_outstanding_balance(self, mine, db):
        customer, vehicle, client = mine
        await _add_invoice(
            db, customer, vehicle, total=500.0, amount_paid=200.0,
            status=InvoiceStatus.PARTIALLY_PAID.value,
        )

        body = (await client.get(f"{PORTAL}/invoices")).json()[0]

        assert body["total"] == 500.0
        assert body["amount_paid"] == 200.0
        assert body["balance"] == 300.0

    async def test_overdue_invoice_reports_days_late(self, mine, db):
        customer, vehicle, client = mine
        await _add_invoice(
            db, customer, vehicle, total=100.0, due_date=_today() - timedelta(days=10)
        )

        body = (await client.get(f"{PORTAL}/invoices")).json()[0]

        assert body["is_overdue"] is True
        assert body["days_overdue"] == 10

    async def test_a_paid_invoice_is_never_overdue(self, mine, db):
        """Old paper with a cleared balance is not late."""
        customer, vehicle, client = mine
        await _add_invoice(
            db, customer, vehicle, total=100.0, amount_paid=100.0,
            status=InvoiceStatus.PAID.value,
            due_date=_today() - timedelta(days=40),
        )

        body = (await client.get(f"{PORTAL}/invoices")).json()[0]

        assert body["is_overdue"] is False
        assert body["days_overdue"] == 0
        assert body["balance"] == 0.0

    async def test_unpaid_filter(self, mine, db):
        customer, vehicle, client = mine
        await _add_invoice(db, customer, vehicle, total=100.0)
        await _add_invoice(
            db, customer, vehicle, total=100.0,
            status=InvoiceStatus.PAID.value, amount_paid=100.0,
        )

        assert len((await client.get(f"{PORTAL}/invoices")).json()) == 2
        assert len((await client.get(f"{PORTAL}/invoices?unpaid_only=true")).json()) == 1

    async def test_estimate_reports_approved_and_total_separately(self, mine, db):
        """A declined line is not going on the bill, so it is not in approved_total."""
        customer, vehicle, client = mine
        await _add_estimate(
            db, customer, vehicle, item_status=EstimateItemStatus.DECLINED.value
        )

        body = (await client.get(f"{PORTAL}/estimates")).json()[0]

        assert body["total"] == 320.0
        assert body["approved_total"] == 0.0

    async def test_expired_estimate_is_flagged(self, mine, db):
        customer, vehicle, client = mine
        await _add_estimate(db, customer, vehicle, valid_until=_today() - timedelta(days=2))

        body = (await client.get(f"{PORTAL}/estimates")).json()[0]

        assert body["is_expired"] is True
        assert all(not line["can_decide"] for line in body["items"])


# --- summary -----------------------------------------------------------------


class TestPortalSummary:
    async def test_summary_counts_only_this_customers_records(self, mine, theirs, db):
        customer, vehicle, client = mine
        their_customer, their_vehicle, _ = theirs

        await _add_invoice(db, customer, vehicle, total=400.0)
        # A large bill for somebody else must not move any of these figures.
        await _add_invoice(db, their_customer, their_vehicle, total=9000.0)

        body = (await client.get(f"{PORTAL}/")).json()

        assert body["customer_id"] == str(customer.id)
        assert body["vehicle_count"] == 1
        assert body["open_balance"] == 400.0
        assert body["awaiting_payment"] == 1
        assert body["overdue_balance"] == 0.0

    async def test_summary_flags_what_needs_action(self, mine, db):
        customer, vehicle, client = mine
        await _add_estimate(db, customer, vehicle)
        await _add_invoice(
            db, customer, vehicle, total=100.0, due_date=_today() - timedelta(days=3)
        )

        body = (await client.get(f"{PORTAL}/")).json()

        assert body["awaiting_approval"] == 1
        assert body["awaiting_payment"] == 1
        assert body["overdue_balance"] == 100.0

    async def test_summary_counts_ready_for_pickup(self, mine, db):
        customer, vehicle, client = mine
        db.add(
            RepairOrder(
                ro_number="RO-READY",
                customer_id=customer.id,
                vehicle_id=vehicle.id,
                status=RepairOrderStatus.QC_PASSED.value,
            )
        )
        await db.commit()

        assert (await client.get(f"{PORTAL}/")).json()["ready_for_pickup"] == 1

    async def test_summary_is_empty_rather_than_wrong(self, theirs, db):
        """A customer with nothing on their account still gets a coherent page."""
        _, _, client = theirs
        body = (await client.get(f"{PORTAL}/")).json()

        assert body["vehicle_count"] == 1
        assert body["open_balance"] == 0.0
        assert body["awaiting_approval"] == 0
        assert body["next_appointment"] is None

    async def test_summary_shows_the_next_appointment(self, mine, db):
        customer, vehicle, client = mine
        await _add_appointment(db, customer, vehicle, days_ahead=2)

        body = (await client.get(f"{PORTAL}/")).json()

        assert body["next_appointment"] is not None
        assert body["next_appointment"]["service_type"] == "Brake inspection"

    async def test_appointments_can_be_filtered_to_upcoming(self, mine, db):
        customer, vehicle, client = mine
        await _add_appointment(db, customer, vehicle, days_ahead=2)
        await _add_appointment(
            db, customer, vehicle, days_ahead=-2, status="COMPLETED"
        )

        assert len((await client.get(f"{PORTAL}/appointments")).json()) == 2
        upcoming = (await client.get(f"{PORTAL}/appointments?upcoming_only=true")).json()
        assert len(upcoming) == 1


# --- documents ---------------------------------------------------------------


class TestPortalDocuments:
    async def test_estimate_document_downloads(self, mine, db):
        customer, vehicle, client = mine
        estimate = await _add_estimate(db, customer, vehicle)

        response = await client.get(f"{PORTAL}/estimates/{estimate.id}/document")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert estimate.estimate_number in response.headers["content-disposition"]
        assert "Brake pads, front axle" in response.text
        assert "320.00" in response.text

    async def test_invoice_document_downloads(self, mine, db):
        customer, vehicle, client = mine
        invoice = await _add_invoice(db, customer, vehicle, total=750.0)

        response = await client.get(f"{PORTAL}/invoices/{invoice.id}/document")

        assert response.status_code == 200
        assert invoice.invoice_number in response.headers["content-disposition"]
        assert "750.00" in response.text
        assert "Balance due" in response.text

    async def test_document_html_is_escaped(self, mine, db):
        """Customer text must not be able to break the document's markup."""
        customer, vehicle, client = mine
        estimate = await _add_estimate(
            db, customer, vehicle, description="<script>alert('xss')</script>"
        )

        response = await client.get(f"{PORTAL}/estimates/{estimate.id}/document")

        assert "<script>alert" not in response.text
        assert "&lt;script&gt;" in response.text

    async def test_another_customers_document_is_not_found(self, mine, theirs, db):
        _, _, client = mine
        their_customer, their_vehicle, _ = theirs
        invoice = await _add_invoice(db, their_customer, their_vehicle)

        response = await client.get(f"{PORTAL}/invoices/{invoice.id}/document")

        assert response.status_code == 404

    async def test_paid_invoice_document_shows_no_balance(self, mine, db):
        customer, vehicle, client = mine
        invoice = await _add_invoice(
            db, customer, vehicle, total=500.0, amount_paid=500.0,
            status=InvoiceStatus.PAID.value,
        )

        text = (await client.get(f"{PORTAL}/invoices/{invoice.id}/document")).text

        assert "Balance due" not in text
        assert "500.00" in text


# --- estimate decisions ------------------------------------------------------


class TestEstimateDecisions:
    async def test_customer_approves_a_line(self, mine, db):
        customer, vehicle, client = mine
        estimate = await _add_estimate(db, customer, vehicle)
        item = estimate.items[0]

        response = await client.post(
            f"{PORTAL}/estimates/{estimate.id}/items/{item.id}/decision",
            json={"decision": "APPROVED"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["item_status"] == "APPROVED"
        assert body["approved_total"] == 320.0
        assert body["remaining_to_decide"] == 0
        assert body["estimate_status_view"]["label"]

    async def test_customer_declines_a_line(self, mine, db):
        customer, vehicle, client = mine
        estimate = await _add_estimate(db, customer, vehicle)

        response = await client.post(
            f"{PORTAL}/estimates/{estimate.id}/items/{estimate.items[0].id}/decision",
            json={"decision": "DECLINED"},
        )

        assert response.status_code == 200
        assert response.json()["approved_total"] == 0.0

    async def test_partial_decision_keeps_the_estimate_open(self, mine, db):
        customer, vehicle, client = mine
        estimate = await _add_estimate(db, customer, vehicle)
        await _add_estimate_line(
            db, estimate, description="Optional: paint repair", line_total=80.0
        )

        response = await client.post(
            f"{PORTAL}/estimates/{estimate.id}/items/{estimate.items[0].id}/decision",
            json={"decision": "APPROVED"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["approved_total"] == 320.0
        assert body["remaining_to_decide"] == 1
        assert body["estimate_status"] == EstimateStatus.PARTIALLY_APPROVED.value

    async def test_decision_survives_a_reload(self, mine, db):
        """The decision is recorded on the estimate, not in the response."""
        customer, vehicle, client = mine
        estimate = await _add_estimate(db, customer, vehicle)

        await client.post(
            f"{PORTAL}/estimates/{estimate.id}/items/{estimate.items[0].id}/decision",
            json={"decision": "APPROVED"},
        )

        body = (await client.get(f"{PORTAL}/estimates/{estimate.id}")).json()
        assert body["status"] == EstimateStatus.APPROVED.value
        assert body["items"][0]["status"] == EstimateItemStatus.APPROVED.value
        assert body["approved_total"] == 320.0

    async def test_expired_estimate_refuses_a_decision(self, mine, db):
        customer, vehicle, client = mine
        estimate = await _add_estimate(
            db, customer, vehicle, valid_until=_today() - timedelta(days=1)
        )

        response = await client.post(
            f"{PORTAL}/estimates/{estimate.id}/items/{estimate.items[0].id}/decision",
            json={"decision": "APPROVED"},
        )

        assert response.status_code >= 400

    async def test_cannot_decide_on_another_customers_estimate(
        self, mine, theirs, db
    ):
        _, _, client = mine
        their_customer, their_vehicle, _ = theirs
        estimate = await _add_estimate(db, their_customer, their_vehicle)

        response = await client.post(
            f"{PORTAL}/estimates/{estimate.id}/items/{estimate.items[0].id}/decision",
            json={"decision": "APPROVED"},
        )

        assert response.status_code == 404

    async def test_expired_estimates_are_not_listed_as_awaiting_decision(
        self, mine, db
    ):
        """Listing an expired estimate as "open" asks for a decision that is refused."""
        customer, vehicle, client = mine
        await _add_estimate(db, customer, vehicle, valid_until=_today() - timedelta(days=1))
        await _add_estimate(db, customer, vehicle)

        assert len((await client.get(f"{PORTAL}/estimates")).json()) == 2
        open_only = (await client.get(f"{PORTAL}/estimates?open_only=true")).json()

        assert len(open_only) == 1
        assert open_only[0]["is_expired"] is False


# --- vehicle history ---------------------------------------------------------


class TestVehicleHistory:
    async def test_history_is_scoped_to_the_vehicle(self, mine, theirs, db):
        customer, vehicle, client = mine
        their_customer, their_vehicle, _ = theirs

        await _add_invoice(db, customer, vehicle, total=250.0)
        await _add_invoice(db, their_customer, their_vehicle, total=700.0)

        body = (await client.get(f"{PORTAL}/vehicles/{vehicle.id}")).json()

        assert body["vehicle"]["id"] == str(vehicle.id)
        assert [i["total"] for i in body["invoices"]] == [250.0]

    async def test_spend_counts_settled_invoices_only(self, mine, db):
        """A draft or written-off bill is not money the customer spent."""
        customer, vehicle, client = mine
        await _add_invoice(db, customer, vehicle, total=100.0)
        await _add_invoice(
            db, customer, vehicle, total=200.0,
            status=InvoiceStatus.PAID.value, amount_paid=200.0,
        )
        await _add_invoice(
            db, customer, vehicle, total=400.0, status=InvoiceStatus.VOID.value
        )

        body = (await client.get(f"{PORTAL}/vehicles/{vehicle.id}")).json()

        assert body["total_spent"] == 200.0

    async def test_history_counts_visits(self, mine, db):
        customer, vehicle, client = mine
        db.add(
            RepairOrder(
                ro_number="RO-1",
                customer_id=customer.id,
                vehicle_id=vehicle.id,
                status=RepairOrderStatus.DELIVERED.value,
            )
        )
        db.add(
            RepairOrder(
                ro_number="RO-2",
                customer_id=customer.id,
                vehicle_id=vehicle.id,
                status=RepairOrderStatus.IN_PROGRESS.value,
            )
        )
        await db.commit()

        body = (await client.get(f"{PORTAL}/vehicles/{vehicle.id}")).json()

        assert body["visit_count"] == 2
        assert {o["ro_number"] for o in body["repair_orders"]} == {"RO-1", "RO-2"}

    async def test_history_of_another_customers_vehicle_is_not_found(self, mine, theirs):
        _, _, client = mine
        _, their_vehicle, _ = theirs

        response = await client.get(f"{PORTAL}/vehicles/{their_vehicle.id}")

        assert response.status_code == 404


# --- booking a slot ----------------------------------------------------------


class TestPortalAppointments:
    async def test_a_customer_books_their_own_vehicle(self, mine, db):
        """The third thing a customer does here, and the last one they could do.

        ``appointments:write`` is the customer's permission, so the shop's diary
        is where it belongs. The booking lands with the shop's own id generation
        and status machine, which is what stops the portal growing a second
        calendar that disagrees with the first.
        """
        customer, vehicle, client = mine
        when = datetime.now(UTC) + timedelta(days=4)

        response = await client.post(
            f"{PORTAL}/appointments",
            json={
                "vehicle_id": str(vehicle.id),
                "service_type": "BRAKE_SERVICE",
                "scheduled_start": when.isoformat(),
                "customer_concern": "Grinding at low speed",
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["vehicle_id"] == str(vehicle.id)
        assert body["service_type"] == "BRAKE_SERVICE"
        # A customer's booking arrives as a request, not a confirmed slot: the
        # front desk confirms the bay, and promising a time here would be
        # promising the shop's diary rather than the customer's wish.
        assert body["status"] == "REQUESTED"
        assert body["status_view"]["label"]
        assert body["bay"] is None

        booked = (
            await db.execute(select(Appointment).where(Appointment.id == uuid.UUID(body["id"])))
        ).scalar_one()
        assert booked.customer_id == customer.id

    async def test_a_booking_for_another_customers_vehicle_is_not_found(self, mine, theirs):
        _, _, client = mine
        _, their_vehicle, _ = theirs
        when = datetime.now(UTC) + timedelta(days=5)

        response = await client.post(
            f"{PORTAL}/appointments",
            json={
                "vehicle_id": str(their_vehicle.id),
                "scheduled_start": when.isoformat(),
            },
        )

        assert response.status_code == 404

    async def test_a_booking_shows_up_in_the_customers_own_list(self, mine):
        _, _, client = mine
        when = datetime.now(UTC) + timedelta(days=6)
        vehicle = (await client.get(f"{PORTAL}/vehicles")).json()[0]

        await client.post(
            f"{PORTAL}/appointments",
            json={
                "vehicle_id": vehicle["id"],
                "scheduled_start": when.isoformat(),
            },
        )

        body = (await client.get(f"{PORTAL}/appointments")).json()
        assert len(body) == 1
        assert body[0]["status"] == "REQUESTED"

    async def test_an_unknown_service_type_is_refused(self, mine):
        _, _, client = mine
        vehicle = (await client.get(f"{PORTAL}/vehicles")).json()[0]
        when = datetime.now(UTC) + timedelta(days=7)

        response = await client.post(
            f"{PORTAL}/appointments",
            json={
                "vehicle_id": vehicle["id"],
                "service_type": "SOMETHING_ELSE",
                "scheduled_start": when.isoformat(),
            },
        )

        assert response.status_code == 422

    async def test_no_customer_id_can_be_supplied(self, mine, theirs, db):
        """The booking is booked against the token's account, whatever the body says."""
        _, _, client = mine
        _, their_customer, _ = theirs
        vehicle = (await client.get(f"{PORTAL}/vehicles")).json()[0]
        when = datetime.now(UTC) + timedelta(days=8)

        response = await client.post(
            f"{PORTAL}/appointments",
            json={
                "vehicle_id": vehicle["id"],
                "customer_id": str(their_customer.id),
                "bay": "Bay 1",
                "scheduled_start": when.isoformat(),
            },
        )

        assert response.status_code == 201
        booked = (
            await db.execute(select(Appointment).where(Appointment.id == uuid.UUID(response.json()["id"])))
        ).scalar_one()
        assert booked.customer_id != their_customer.id
        assert booked.bay is None


# --- filing a request --------------------------------------------------------


class TestPortalServiceRequests:
    async def test_a_customer_files_a_request_against_their_own_account(self, mine, db):
        """Asking the shop for work is the one thing a customer does here.

        It is a write, so it used to be reachable only through the shop's own
        endpoint — which takes a ``customer_id`` in the body, and therefore let a
        customer file against somebody else's account. The portal has no such
        field: the account comes from the token.
        """
        customer, vehicle, client = mine

        response = await client.post(
            f"{PORTAL}/service-requests",
            json={
                "title": "Grinding when braking",
                "description": "Started after the last visit",
                "vehicle_id": str(vehicle.id),
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["title"] == "Grinding when braking"
        assert body["vehicle_id"] == str(vehicle.id)
        assert body["status_view"]["label"]

        filed = (
            await db.execute(select(ServiceRequest).where(ServiceRequest.title == body["title"]))
        ).scalar_one()
        assert filed.customer_id == customer.id

    async def test_the_request_lands_in_the_shop_queue(self, mine, db):
        """The portal is a way in, not a second queue.

        The row is created through the shop's own service, so the front desk sees
        the request with everything else rather than in a place nobody looks.
        """
        _, _, client = mine

        await client.post(f"{PORTAL}/service-requests", json={"title": "Routine service"})

        queued = (
            await db.execute(
                select(ServiceRequest).where(ServiceRequest.title == "Routine service")
            )
        ).scalar_one()
        assert queued.status == "NEW"
        assert queued.service_advisor_notes is None

    async def test_a_customer_id_in_the_body_is_not_where_the_request_lands(self, mine, theirs, db):
        """An extra field cannot redirect the write.

        The schema has no ``customer_id``, so a body carrying one is ignored
        rather than obeyed. If that ever changes, this test is what notices.
        """
        _, _, client = mine
        their_customer, _, _ = theirs

        response = await client.post(
            f"{PORTAL}/service-requests",
            json={
                "title": "Not theirs to file",
                "customer_id": str(their_customer.id),
            },
        )

        assert response.status_code == 201
        filed = (
            await db.execute(
                select(ServiceRequest).where(ServiceRequest.title == "Not theirs to file")
            )
        ).scalar_one()
        assert filed.customer_id != their_customer.id

    async def test_a_request_against_another_customers_vehicle_is_not_found(self, mine, theirs):
        _, _, client = mine
        _, their_vehicle, _ = theirs

        response = await client.post(
            f"{PORTAL}/service-requests",
            json={"title": "Their car", "vehicle_id": str(their_vehicle.id)},
        )

        assert response.status_code == 404

    async def test_it_shows_up_in_the_customers_own_list(self, mine):
        _, _, client = mine

        await client.post(f"{PORTAL}/service-requests", json={"title": "Windscreen chip"})

        body = (await client.get(f"{PORTAL}/service-requests")).json()
        assert [r["title"] for r in body] == ["Windscreen chip"]


# --- settling a bill ---------------------------------------------------------


class TestPortalPayments:
    async def test_a_customer_settles_part_of_their_own_invoice(self, mine, db):
        """Paying a bill is the other thing a customer does here.

        It is the shop's payment service underneath, so the balance moves by the
        same arithmetic the counter uses and the invoice status lands where it
        would have.
        """
        customer, vehicle, client = mine
        invoice = await _add_invoice(db, customer, vehicle, total=500.0)

        response = await client.post(
            f"{PORTAL}/invoices/{invoice.id}/payments",
            json={"amount": 200.0, "method": "CARD"},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["payment"]["amount"] == 200.0
        assert body["payment"]["invoice_number"] == invoice.invoice_number
        assert body["invoice_status"] == InvoiceStatus.PARTIALLY_PAID.value
        assert body["invoice_balance"] == 300.0
        # Translated alongside, like every other status in the portal.
        assert body["invoice_status_view"]["label"]

        refreshed = (
            await db.execute(select(Invoice).where(Invoice.id == invoice.id))
        ).scalar_one()
        assert refreshed.amount_paid == 200.0
        assert refreshed.balance == 300.0

    async def test_paying_in_full_clears_the_balance(self, mine, db):
        customer, vehicle, client = mine
        invoice = await _add_invoice(db, customer, vehicle, total=120.0)

        response = await client.post(
            f"{PORTAL}/invoices/{invoice.id}/payments",
            json={"amount": 120.0, "method": "CASH"},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["invoice_status"] == InvoiceStatus.PAID.value
        assert body["invoice_balance"] == 0.0

    async def test_the_payment_is_recorded_against_the_customer_not_the_desk(self, mine, db):
        """Money that arrived over the portal was not taken by an employee.

        The audit trail should say who paid, so the recorded-by id is the
        customer's own user rather than the staff member who happens to be logged
        in at the front desk.
        """
        customer, vehicle, client = mine
        invoice = await _add_invoice(db, customer, vehicle, total=80.0)

        await client.post(
            f"{PORTAL}/invoices/{invoice.id}/payments",
            json={"amount": 80.0, "method": "CARD"},
        )

        payment = (await db.execute(select(Payment))).scalar_one()
        assert str(payment.recorded_by_id) == str(customer.user_id)

    async def test_another_customers_invoice_cannot_be_paid(self, mine, theirs, db):
        """404, not 403 — the invoice does not exist as far as this account knows."""
        _, _, client = mine
        their_customer, their_vehicle, _ = theirs
        invoice = await _add_invoice(db, their_customer, their_vehicle, total=500.0)

        response = await client.post(
            f"{PORTAL}/invoices/{invoice.id}/payments",
            json={"amount": 500.0, "method": "CARD"},
        )

        assert response.status_code == 404
        assert (await db.execute(select(func.count(Payment.id)))).scalar_one() == 0

    async def test_more_than_the_balance_is_refused(self, mine, db):
        """The same rule the counter applies, because it is the same service."""
        customer, vehicle, client = mine
        invoice = await _add_invoice(db, customer, vehicle, total=100.0)

        response = await client.post(
            f"{PORTAL}/invoices/{invoice.id}/payments",
            json={"amount": 150.0, "method": "CARD"},
        )

        assert response.status_code == 400
        assert (await db.execute(select(func.count(Payment.id)))).scalar_one() == 0

    async def test_a_draft_invoice_cannot_be_paid_yet(self, mine, db):
        """A bill the shop has not issued is not a bill."""
        customer, vehicle, client = mine
        invoice = await _add_invoice(
            db, customer, vehicle, total=100.0, status=InvoiceStatus.DRAFT.value
        )

        response = await client.post(
            f"{PORTAL}/invoices/{invoice.id}/payments",
            json={"amount": 100.0, "method": "CARD"},
        )

        assert response.status_code == 400

    async def test_a_transfer_without_a_reference_is_refused(self, mine, db):
        """The shop's own rule: an unreferenced transfer cannot be reconciled."""
        customer, vehicle, client = mine
        invoice = await _add_invoice(db, customer, vehicle, total=100.0)

        response = await client.post(
            f"{PORTAL}/invoices/{invoice.id}/payments",
            json={"amount": 100.0, "method": "BANK_TRANSFER"},
        )

        assert response.status_code == 422

    async def test_the_payment_shows_in_the_customers_own_history(self, mine, db):
        customer, vehicle, client = mine
        invoice = await _add_invoice(db, customer, vehicle, total=300.0)

        await client.post(
            f"{PORTAL}/invoices/{invoice.id}/payments",
            json={"amount": 300.0, "method": "CARD"},
        )

        body = (await client.get(f"{PORTAL}/payments")).json()
        assert [p["amount"] for p in body] == [300.0]

    async def test_paying_twice_over_the_same_balance_is_refused(self, mine, db):
        customer, vehicle, client = mine
        invoice = await _add_invoice(db, customer, vehicle, total=100.0)

        first = await client.post(
            f"{PORTAL}/invoices/{invoice.id}/payments",
            json={"amount": 100.0, "method": "CARD"},
        )
        second = await client.post(
            f"{PORTAL}/invoices/{invoice.id}/payments",
            json={"amount": 100.0, "method": "CARD"},
        )

        assert first.status_code == 201
        assert second.status_code == 400
        assert (await db.execute(select(func.count(Payment.id)))).scalar_one() == 1
