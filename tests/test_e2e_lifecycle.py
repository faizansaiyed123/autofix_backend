"""End-to-end tests: one customer, one vehicle, all the way to a settled bill.

Every other suite in this project tests a *seam* -- one state machine, one
permission, one rule. That is the right way to find bugs and the wrong way to
find the ones that matter most here, because the expensive mistakes in a garage
platform are not "the estimate totals are wrong". They are "the estimate cannot be
approved because the RO cannot be raised without something the estimate never
recorded", and no unit test writes that test because nobody thinks to.

So this file drives the whole business through HTTP, as the shop and the customer
would: a customer asks for work in the portal, the shop registers the car, books
it in, inspects it, quotes it, the customer decides line by line, the work is done,
independently inspected, invoiced, paid, and read back out of the portal. Nothing
calls a service directly and nothing writes to the database to make a step
happen, because the seams between the routes, the role permissions and the state
machines are exactly what is being tested.

Two things are asserted along the way that are easy to lose in a happy-path test:

* **The money is the customer's money.** The bill is the approved lines and
  nothing else, so declining an optional line has to be visible in the total.
* **The record survives the fact.** The audit trail is asserted at the end, and
  the notification centre, because the alternative -- a system that does the work
  and cannot say who did it or tell the customer -- is not a system anybody
  trusts with their car.

The dashboard test in this file rides on the same driver, via its ``observe``
hook, because the operational numbers only mean anything *between* the steps.
"""

# ruff: noqa: DTZ011
# The overdue scenario below backdates an invoice's due date, and "ten days ago"
# is a statement about the shop's own calendar day -- which is exactly what
# `date.today()` returns, and what the API compares against. A UTC date here would
# disagree with it for eight hours a day.
from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.customers.models import Customer
from app.invoices.models import Invoice

CUSTOMERS_URL = "/api/v1/customers/"
VEHICLES_URL = "/api/v1/vehicles/"
REQUESTS_URL = "/api/v1/service_requests/"
APPOINTMENTS_URL = "/api/v1/appointments/"
CHECK_INS_URL = "/api/v1/check_ins/"
INSPECTIONS_URL = "/api/v1/inspections/"
ESTIMATES_URL = "/api/v1/estimates/"
RO_URL = "/api/v1/repair_orders/"
LABOR_URL = "/api/v1/labor/"
PART_REQUESTS_URL = "/api/v1/part_requests/"
QC_URL = "/api/v1/qc/"
INVOICES_URL = "/api/v1/invoices/"
PAYMENTS_URL = "/api/v1/payments/"
PORTAL_URL = "/api/v1/portal"
# No trailing slash: the notification centre's collection route is "/", so the
# slash is added where it is used rather than baked into the constant.
NOTIFICATIONS_URL = "/api/v1/notifications"
AUDIT_URL = "/api/v1/audit/"
REPORTS_URL = "/api/v1/reports"

# --- the money, stated once -------------------------------------------------
#
# The estimate quotes three lines and the customer decides each one, so the bill
# at the end of the day has to be arithmetic a reader can do in their head. These
# are the numbers the assertions below use; if the product's arithmetic changes,
# this is the place that should be noticed rather than the place to quietly
# re-baseline a number to whatever came back.
TAX_RATE = 0.0825
LABOR_HOURS = 2.5
LABOR_RATE = 95.0
PADS_PRICE = 180.0
FLUSH_PRICE = 95.0  # declined by the customer, so it never reaches the bill

# 2.5h x 95.00 + 180.00 = 417.50 agreed, plus 8.25% tax = 451.94 owed.
LABOUR_COST = round(LABOR_HOURS * LABOR_RATE, 2)
APPROVED_SUBTOTAL = round(LABOR_HOURS * LABOR_RATE + PADS_PRICE, 2)
BILLED_SUBTOTAL = 417.50
BILLED_TAX = 34.44
BILLED_TOTAL = 451.94
QUOTED_TOTAL = 554.78  # everything on the estimate, declined line included

ODOMETER = 86420


# --- fixtures ---------------------------------------------------------------


@pytest.fixture()
async def portal_customer(db: AsyncSession) -> Customer:
    """A customer record with the seeded customer login behind it.

    The seeded ``customer@autofix.demo`` account is a bare ``User``: the portal
    resolves its customer from ``customers.user_id``, so without this row the
    customer's own endpoints answer 404 and half this test cannot be written. The
    ``db`` fixture re-seeds roles, permissions and demo *users* between tests but
    not the demo customer rows, so the link has to be made here.
    """
    user = (
        await db.execute(select(User).where(User.email == "customer@autofix.demo"))
    ).scalar_one()
    customer = Customer(
        first_name="David",
        last_name="Wilson",
        email="customer@autofix.demo",
        phone="+1-503-555-0101",
        preferred_contact="EMAIL",
        customer_status="ACTIVE",
        user_id=user.id,
    )
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    return customer


@pytest.fixture()
async def staff(db: AsyncSession) -> dict[str, str]:
    """The seeded staff logins, so assignments can name real people.

    Every lifecycle step is performed by somebody, and several rules turn on
    *which* somebody -- a technician may not approve the part they asked for, and
    may not inspect their own work. Naming the people by their seeded login is
    what makes those rules testable rather than incidental.
    """
    emails = {
        "advisor": "manager@autofix.demo",
        "technician": "tech@autofix.demo",
        "parts": "parts@autofix.demo",
    }
    rows = (
        (await db.execute(select(User).where(User.email.in_(list(emails.values())))))
        .scalars()
        .all()
    )
    by_email = {user.email: str(user.id) for user in rows}
    return {label: by_email[email] for label, email in emails.items()}


# --- helpers ----------------------------------------------------------------


async def _ok(response, *expected: int) -> dict:
    """Assert a response carried one of ``expected`` statuses and return its body.

    Defaults to 200, so a call only names the status when a write is the one that
    matters -- and when it does, the step states it out loud.
    """
    assert response.status_code in (expected or (200,)), (
        f"{response.request.url}: {response.text}"
    )
    return response.json()


def _tomorrow_morning() -> str:
    """A slot comfortably in the future, so nothing here races the clock."""
    slot = datetime.now(UTC).replace(hour=9, minute=0, second=0, microsecond=0)
    return (slot + timedelta(days=1)).isoformat()


StageHook = Callable[[str, dict[str, Any]], Awaitable[None]]


async def run_the_whole_visit(
    *,
    advisor: AsyncClient,
    technician: AsyncClient,
    parts_clerk: AsyncClient,
    customer: AsyncClient,
    record: Customer,
    staff: dict[str, str],
    observe: StageHook | None = None,
) -> dict[str, Any]:
    """Take one customer from a phone call to a settled bill, over HTTP only.

    ``observe`` is awaited after every stage with the stage name and the record
    so far. The dashboard test needs the shop's own numbers *between* these steps
    -- the moment the repair order is waiting on quality control is precisely the
    moment a test that only inspects the end state would miss -- so the hook is
    how one driver serves two questions instead of two drivers drifting apart.
    """
    facts: dict[str, Any] = {}

    async def stage(name: str) -> None:
        if observe is not None:
            await observe(name, facts)

    # 1. The customer asks for work, from their own account. No customer id in
    #    the body: the portal derives it from the token, which is the whole reason
    #    a customer can use this endpoint without being able to file against
    #    somebody else's car.
    request = await _ok(
        await customer.post(
            f"{PORTAL_URL}/service-requests",
            json={
                "title": "Brakes squeal under load",
                "description": "Started after the last motorway trip, worse when cold.",
                "priority": "HIGH",
            },
        ),
        201,
    )
    assert request["status"] == "NEW"
    facts["request"] = request
    await stage("requested")

    # 2. The shop registers the car. Customers hold no vehicles:write, and that is
    #    correct -- who owns which car is the shop's record, not the customer's to
    #    assert.
    vehicle = await _ok(
        await advisor.post(
            VEHICLES_URL,
            json={
                "customer_id": str(record.id),
                "vin": "1HGCM82633A004352",
                "license_plate": "ORB-2210",
                "make": "Honda",
                "model": "Accord",
                "year": 2019,
                "color": "Silver",
                "mileage": ODOMETER,
                "fuel_type": "GASOLINE",
            },
        ),
        201,
    )
    # 3. ...and attaches it to the request, which is how the diary later knows
    #    which car the booking is for without asking the customer again.
    facts["vehicle"] = vehicle
    facts["request"] = await _ok(
        await advisor.patch(
            f"{REQUESTS_URL}{request['id']}", json={"vehicle_id": vehicle["id"]}
        )
    )

    # 4. The advisor takes the request off the pile and approves it. Two steps,
    #    because the state machine says an untriaged request is not a booking.
    for status_name in ("IN_REVIEW", "APPROVED"):
        facts["request"] = await _ok(
            await advisor.patch(
                f"{REQUESTS_URL}{request['id']}/status", params={"status": status_name}
            )
        )
    await stage("approved")

    # 5. The approved request becomes a booking. This is the conversion that
    #    closes the intake loop: one transaction writes the appointment and marks
    #    the request CONVERTED, so a clashing slot cannot leave a request marked
    #    converted with nothing on the calendar.
    appointment = await _ok(
        await advisor.post(
            f"{APPOINTMENTS_URL}from-service-request/{request['id']}",
            json={
                "scheduled_start": _tomorrow_morning(),
                "duration_minutes": 120,
                "service_type": "BRAKE_SERVICE",
                "bay": "BAY-1",
                "advisor_id": staff["advisor"],
                "technician_id": staff["technician"],
            },
        ),
        201,
    )
    assert appointment["status"] == "CONFIRMED"
    assert appointment["vehicle_id"] == vehicle["id"]
    facts["appointment"] = appointment
    facts["request"] = await _ok(await advisor.get(f"{REQUESTS_URL}{request['id']}"))
    assert facts["request"]["status"] == "CONVERTED"
    await stage("booked")

    # 6. The car arrives. Checked in against the booking, with the odometer read
    #    off the car rather than copied from the customer record.
    checkin = await _ok(
        await advisor.post(
            CHECK_INS_URL,
            json={
                "vehicle_id": vehicle["id"],
                "customer_id": str(record.id),
                "odometer": ODOMETER,
                "checkin_type": "APPOINTMENT",
                "service_advisor_id": staff["advisor"],
                "notes": "Customer waiting in reception.",
            },
        ),
        201,
    )
    facts["checkin"] = checkin
    facts["appointment"] = await _ok(
        await advisor.patch(
            f"{APPOINTMENTS_URL}{appointment['id']}/status",
            json={"status": "CHECKED_IN"},
        )
    )
    await stage("checked_in")

    # 7. The digital inspection. One item is a photo, which is also what makes the
    #    QC documentation check pass later -- the photographs taken here are the
    #    photographs QC looks for.
    inspection = await _ok(
        await technician.post(
            INSPECTIONS_URL,
            json={
                "vehicle_id": vehicle["id"],
                "customer_id": str(record.id),
                "checkin_id": checkin["id"],
                "technician_id": staff["technician"],
                "mileage": ODOMETER,
                "items": [
                    {
                        "category": "Brakes",
                        "item_name": "Front brake pads",
                        "status": "ATTENTION",
                        "measurement": "3mm",
                        "recommendation": "REPLACE",
                        "notes": "Past the wear line on the inner edge.",
                        "photo_url": "https://cdn.autofix/e2e/front-pad.jpg",
                        "photo_caption": "Worn inner edge",
                    },
                    {
                        "category": "Brakes",
                        "item_name": "Front brake rotors",
                        "status": "ATTENTION",
                        "measurement": "11.2mm",
                        "recommendation": "MONITOR",
                    },
                    {
                        "category": "Fluids",
                        "item_name": "Brake fluid",
                        "status": "GOOD",
                    },
                ],
            },
        ),
        201,
    )
    for status_name in ("IN_PROGRESS", "COMPLETED"):
        inspection = await _ok(
            await technician.patch(
                f"{INSPECTIONS_URL}{inspection['id']}/status",
                params={"status": status_name},
            )
        )
    assert inspection["overall_condition"] == "YELLOW"
    facts["inspection"] = inspection
    await stage("inspected")

    # 8. The estimate is written against the inspection and the request, so the
    #    quote is traceable back to the two things that prompted it.
    estimate = await _ok(
        await advisor.post(
            ESTIMATES_URL,
            json={
                "customer_id": str(record.id),
                "vehicle_id": vehicle["id"],
                "inspection_id": inspection["id"],
                "service_request_id": request["id"],
                "tax_rate": TAX_RATE,
                "notes": "Safety work first; the fluid flush is advisory.",
                "items": [
                    {
                        "item_type": "LABOR",
                        "description": "Brake pad and rotor replacement",
                        "labor_hours": LABOR_HOURS,
                        "labor_rate": LABOR_RATE,
                    },
                    {
                        "item_type": "PART",
                        "description": "Front brake pad set",
                        "part_number": "BP-1042",
                        "part_name": "Ceramic brake pad set",
                        "quantity": 1,
                        "unit_price": PADS_PRICE,
                    },
                    {
                        "item_type": "PART",
                        "description": "Brake fluid flush",
                        "quantity": 1,
                        "unit_price": FLUSH_PRICE,
                        "is_optional": True,
                        "notes": "Fluid is two years old; recommended, not urgent.",
                    },
                ],
            },
        ),
        201,
    )
    assert estimate["status"] == "DRAFT"
    assert estimate["subtotal"] == round(LABOR_HOURS * LABOR_RATE + PADS_PRICE + FLUSH_PRICE, 2)
    assert estimate["total"] == QUOTED_TOTAL
    facts["estimate"] = estimate

    # 9. Sent. This is the moment the customer is asked to do something, so it is
    #    the moment they are told: the notice is written with the same save that
    #    commits the status, and cannot outlive an estimate that rolled back.
    estimate = await _ok(await advisor.post(f"{ESTIMATES_URL}{estimate['id']}/send"))
    assert estimate["status"] == "SENT"
    assert estimate["sent_at"] is not None
    facts["estimate"] = estimate
    await stage("sent")

    # 10. The customer decides, line by line, from their own account. Decisions
    #     are per line so that approving the safety work does not force approving
    #     the advisory extra -- which is the behaviour the estimate's own status
    #     machine reports as PARTIALLY_APPROVED while lines are outstanding.
    lines = {item["description"]: item for item in estimate["items"]}
    decision_path = f"{PORTAL_URL}/estimates/{estimate['id']}/items"
    safety = await _ok(
        await customer.post(
            f"{decision_path}/{lines['Brake pad and rotor replacement']['id']}/decision",
            json={"decision": "APPROVED"},
        )
    )
    assert safety["estimate_status"] == "PARTIALLY_APPROVED"
    assert safety["remaining_to_decide"] == 2

    await _ok(
        await customer.post(
            f"{decision_path}/{lines['Front brake pad set']['id']}/decision",
            json={"decision": "APPROVED"},
        )
    )
    declined = await _ok(
        await customer.post(
            f"{decision_path}/{lines['Brake fluid flush']['id']}/decision",
            json={
                "decision": "DECLINED",
                "notes": "Not this visit, thanks.",
            },
        )
    )
    assert declined["remaining_to_decide"] == 0
    assert declined["approved_total"] == BILLED_TOTAL
    # Every line has now been decided and not all of them were declined, so the
    # estimate reads APPROVED -- which is the state a repair order may be built on.
    assert declined["estimate_status"] == "APPROVED"
    facts["estimate"] = await _ok(await advisor.get(f"{ESTIMATES_URL}{estimate['id']}"))
    assert facts["estimate"]["approved_total"] == BILLED_TOTAL
    await stage("decided")

    # 11. The repair order, raised from the accepted estimate with no task list of
    #     its own: the breakdown is generated from the approved lines, so a task
    #     exists for the work that was agreed and none for the line that was
    #     declined.
    order = await _ok(
        await advisor.post(
            RO_URL,
            json={
                "customer_id": str(record.id),
                "vehicle_id": vehicle["id"],
                "estimate_id": estimate["id"],
                "appointment_id": appointment["id"],
                "advisor_id": staff["advisor"],
                "technician_id": staff["technician"],
                "odometer_in": ODOMETER,
                "bay": "BAY-1",
            },
        ),
        201,
    )
    assert order["status"] == "DRAFT"
    assert [task["description"] for task in order["tasks"]] == [
        "Brake pad and rotor replacement",
        "Front brake pad set",
    ]
    facts["order"] = order

    # 12. Approved and started. The advisor authorises; the technician moves it.
    for status_name in ("APPROVED", "IN_PROGRESS"):
        order = await _ok(
            await technician.patch(
                f"{RO_URL}{order['id']}/status", params={"status": status_name}
            )
        )
    assert order["status"] == "IN_PROGRESS"
    assert order["started_at"] is not None
    facts["order"] = order
    await stage("started")

    # 13. Labour, while the order is still live -- the service refuses labour on a
    #     finished order, which is the rule that keeps the invoice matching work
    #     that actually happened.
    labour = await _ok(
        await technician.post(
            LABOR_URL,
            json={
                "repair_order_id": order["id"],
                "repair_task_id": order["tasks"][0]["id"],
                "technician_id": staff["technician"],
                "description": "Brake pad and rotor replacement",
                "actual_hours": LABOR_HOURS,
                "billable_hours": LABOR_HOURS,
                "hourly_rate": LABOR_RATE,
            },
        ),
        201,
    )
    assert labour["labor_cost"] == LABOUR_COST
    facts["labour"] = labour
    await stage("labour")

    # 14. The part is requested by the technician who needs it, and decided by
    #     somebody else: the technician who raised a request cannot approve it.
    part_request = await _ok(
        await technician.post(
            PART_REQUESTS_URL,
            json={
                "repair_order_id": order["id"],
                "repair_task_id": order["tasks"][1]["id"],
                "part_number": "BP-1042",
                "part_name": "Ceramic brake pad set",
                "quantity": 1,
                "reason": "Pads worn past the wear line",
            },
        ),
        201,
    )
    assert part_request["status"] == "PENDING"
    # The stage is named before the parts department answers, because the queue a
    # request sits in is exactly the thing the dashboard is supposed to show.
    await stage("part_requested")
    decided_part = await _ok(
        await parts_clerk.post(
            f"{PART_REQUESTS_URL}{part_request['id']}/decision",
            json={"decision": "APPROVED", "decision_reason": "On the shelf"},
        )
    )
    assert decided_part["status"] == "APPROVED"
    facts["part_request"] = await _ok(
        await parts_clerk.post(f"{PART_REQUESTS_URL}{part_request['id']}/fulfil")
    )
    assert facts["part_request"]["status"] == "FULFILLED"
    await stage("part_staged")

    # 15. Tasks done, then the order. The order cannot be completed while a task
    #     is open, so this is two steps rather than one.
    for task in order["tasks"]:
        await _ok(
            await technician.patch(
                f"{RO_URL}{order['id']}/tasks/{task['id']}/status",
                params={"status": "COMPLETED"},
            )
        )
    facts["order"] = await _ok(
        await technician.patch(
            f"{RO_URL}{order['id']}/status", params={"status": "COMPLETED"}
        )
    )
    assert facts["order"]["completed_at"] is not None
    await stage("completed")

    # 16. Quality control, by somebody who did not do the work. The technician's
    #     own attempt is refused first, on purpose: an end-to-end test that only
    #     ever took the happy path would pass against a build where the
    #     independence rule had quietly been deleted.
    refused = await technician.post(QC_URL, json={"repair_order_id": order["id"]})
    assert refused.status_code == 400
    assert "cannot run quality control" in refused.json()["detail"]

    check = await _ok(
        await advisor.post(
            QC_URL,
            json={"repair_order_id": order["id"], "notes": "Road test and re-measure"},
        ),
        201,
    )
    assert check["status"] == "IN_PROGRESS"
    assert check["attempt_number"] == 1
    assert check["inspector_id"] == staff["advisor"]
    assert check["passed"] is True
    facts["check"] = check
    facts["queue"] = await _ok(await advisor.get(f"{QC_URL}queue"))
    await stage("awaiting_qc")

    passed = await _ok(await advisor.post(f"{QC_URL}{check['id']}/pass"))
    assert passed["status"] == "PASSED"
    facts["check"] = passed
    facts["order"] = await _ok(await advisor.get(f"{RO_URL}{order['id']}"))
    assert facts["order"]["status"] == "QC_PASSED"
    await stage("qc_passed")

    # 17. The bill. The approved estimate lines are copied on automatically and
    #     frozen; the declined line is not a debt, so it is not on it.
    invoice = await _ok(
        await advisor.post(
            INVOICES_URL,
            json={"repair_order_id": order["id"], "notes": "Paid in full by card."},
        ),
        201,
    )
    assert invoice["status"] == "DRAFT"
    assert invoice["subtotal"] == BILLED_SUBTOTAL
    assert invoice["tax_amount"] == BILLED_TAX
    assert invoice["total"] == BILLED_TOTAL
    assert invoice["estimate_id"] == estimate["id"]
    assert [item["description"] for item in invoice["items"]] == [
        "Brake pad and rotor replacement",
        "Front brake pad set",
    ]
    assert all(item["source"] == "ESTIMATE" for item in invoice["items"])
    facts["invoice"] = invoice
    await stage("invoiced")

    # 18. Issued, which is the point the balance becomes owed and the lines stop
    #     being editable: the customer now holds the document.
    invoice = await _ok(await advisor.post(f"{INVOICES_URL}{invoice['id']}/issue"))
    assert invoice["status"] == "ISSUED"
    assert invoice["issued_at"] is not None
    facts["invoice"] = invoice
    await stage("issued")

    # 19. Money in, to the cent. An invoice reaches PAID only through a recorded
    #     payment, so this is the only way the last state in the chain is reached.
    payment = await _ok(
        await advisor.post(
            PAYMENTS_URL,
            json={
                "invoice_id": invoice["id"],
                "amount": BILLED_TOTAL,
                "method": "CARD",
                "reference": "AUTH 4471",
            },
        ),
        201,
    )
    assert payment["status"] == "RECORDED"
    facts["payment"] = payment
    facts["invoice"] = await _ok(await advisor.get(f"{INVOICES_URL}{invoice['id']}"))
    assert facts["invoice"]["status"] == "PAID"
    assert facts["invoice"]["amount_paid"] == BILLED_TOTAL
    assert facts["invoice"]["balance"] == 0.0
    await stage("paid")

    return facts


# --- the whole visit --------------------------------------------------------


class TestFullLifecycle:
    @pytest.mark.asyncio
    async def test_one_visit_from_enquiry_to_settled_bill(
        self,
        db: AsyncSession,
        manager_client: AsyncClient,
        technician_client: AsyncClient,
        parts_client: AsyncClient,
        customer_client: AsyncClient,
        owner_client: AsyncClient,
        portal_customer: Customer,
        staff: dict[str, str],
    ):
        """Every stage of one visit, and the record it leaves behind.

        The assertion at the end is deliberately about *what the customer can
        see and what the shop can prove*, not about internal statuses. A system
        that moved every row to the right enum while showing an empty portal and
        an audit log with a hole where the money was would pass a status-only
        test, and it would still be a system nobody could run a garage on.
        """
        facts = await run_the_whole_visit(
            advisor=manager_client,
            technician=technician_client,
            parts_clerk=parts_client,
            customer=customer_client,
            record=portal_customer,
            staff=staff,
        )

        order = facts["order"]
        estimate = facts["estimate"]
        invoice = facts["invoice"]
        vehicle = facts["vehicle"]

        # -- the customer's account ------------------------------------------
        summary = await _ok(await customer_client.get(f"{PORTAL_URL}/"))
        assert summary["customer_id"] == str(portal_customer.id)
        assert summary["vehicle_count"] == 1
        assert summary["awaiting_approval"] == 0
        assert summary["awaiting_payment"] == 0
        # Settled in full, so nothing is owed -- and the order is finished and
        # inspected, which is the one thing still waiting on the customer.
        assert summary["open_balance"] == 0.0
        assert summary["overdue_balance"] == 0.0
        assert summary["ready_for_pickup"] == 1

        customer_estimate = await _ok(
            await customer_client.get(f"{PORTAL_URL}/estimates/{estimate['id']}")
        )
        assert customer_estimate["status"] == "APPROVED"
        assert customer_estimate["approved_total"] == BILLED_TOTAL
        # The declined line is still shown, marked, and no longer decidable: the
        # customer can see what they said no to rather than finding it vanish.
        lines = {line["description"]: line for line in customer_estimate["items"]}
        assert lines["Brake fluid flush"]["status"] == "DECLINED"
        assert lines["Brake fluid flush"]["can_decide"] is False
        assert lines["Brake pad and rotor replacement"]["status"] == "APPROVED"

        customer_invoice = await _ok(
            await customer_client.get(f"{PORTAL_URL}/invoices/{invoice['id']}")
        )
        assert customer_invoice["status"] == "PAID"
        assert customer_invoice["total"] == BILLED_TOTAL
        assert customer_invoice["balance"] == 0.0
        assert customer_invoice["is_overdue"] is False

        history = await _ok(
            await customer_client.get(f"{PORTAL_URL}/vehicles/{vehicle['id']}")
        )
        assert [ro["ro_number"] for ro in history["repair_orders"]] == [order["ro_number"]]
        assert history["visit_count"] == 1
        assert len(history["inspections"]) == 1
        assert len(history["service_requests"]) == 1
        # Spend is counted from settled bills only, so it agrees with the money
        # the shop says it took.
        assert history["total_spent"] == BILLED_TOTAL

        portal_payments = await _ok(await customer_client.get(f"{PORTAL_URL}/payments"))
        assert [p["amount"] for p in portal_payments] == [BILLED_TOTAL]
        assert portal_payments[0]["invoice_number"] == invoice["invoice_number"]

        # -- the shop's own record of the money -------------------------------
        booked = await _ok(await manager_client.get(f"{RO_URL}{order['id']}/summary"))
        assert booked["counts"]["completed"] == 2
        assert booked["counts"]["pending"] == 0

        # -- notifications -----------------------------------------------------
        inbox = await _ok(await customer_client.get(f"{NOTIFICATIONS_URL}/"))
        types = Counter(n["notification_type"] for n in inbox["notifications"])
        assert types["ESTIMATE_READY"] == 1
        assert types["INVOICE_ISSUED"] == 1
        pointed = {n["entity_type"]: n["entity_id"] for n in inbox["notifications"]}
        assert str(pointed["estimate"]) == estimate["id"]
        assert str(pointed["invoice"]) == invoice["id"]
        # Both notices are still unread: nothing has quietly marked them read on
        # the way past.
        assert inbox["unread_count"] == 2

        # -- the audit trail --------------------------------------------------
        # The audit log is the only thing that survives the people involved, so
        # the entries have to name the record, the action *and* the actor, or the
        # trail is a list of timestamps.
        ro_entries = await _ok(
            await owner_client.get(
                AUDIT_URL, params={"entity_type": "repair_order", "entity_id": order["id"]}
            )
        )
        actions = Counter(entry["action"] for entry in ro_entries["data"])
        assert actions["CREATE"] == 1
        # One entry per order status change, plus one per task status change.
        assert actions["STATUS_CHANGE"] == 5
        assert {entry["entity_label"] for entry in ro_entries["data"]} == {order["ro_number"]}
        assert ro_entries["meta"]["total"] == 6

        estimate_history = await _ok(
            await owner_client.get(f"{AUDIT_URL}entity/estimate/{estimate['id']}")
        )
        estimate_actions = Counter(entry["action"] for entry in estimate_history["entries"])
        assert estimate_actions["CREATE"] == 1
        assert estimate_actions["SEND"] == 1
        # One entry per line the customer decided on, because each decision is a
        # separate thing somebody did to the customer's agreement.
        assert estimate_actions["DECIDE"] == 3
        assert estimate_history["entity_label"] == estimate["estimate_number"]

        invoice_entries = await _ok(
            await owner_client.get(
                AUDIT_URL, params={"entity_type": "invoice", "entity_id": invoice["id"]}
            )
        )
        issue = next(e for e in invoice_entries["data"] if e["action"] == "ISSUE")
        assert issue["changes"]["status"] == {"from": "DRAFT", "to": "ISSUED"}
        assert issue["actor_email"] == "manager@autofix.demo"

        payment_entries = await _ok(
            await owner_client.get(
                AUDIT_URL, params={"action": "RECORD_PAYMENT", "entity_type": "payment"}
            )
        )
        assert payment_entries["meta"]["total"] == 1
        assert payment_entries["data"][0]["entity_label"] == "AUTH 4471"

        # The customer's own decisions are audited against the actor who made
        # them, which for a portal decision is the customer -- not the advisor
        # who happens to be reading the log.
        customer_decisions = [
            entry
            for entry in estimate_history["entries"]
            if entry["action"] == "DECIDE"
        ]
        assert {entry["actor_email"] for entry in customer_decisions} == {
            "customer@autofix.demo"
        }

        # The invoice on disk agrees with what was billed, which is the assertion
        # that would catch a line silently dropped between the two.
        stored = (
            await db.execute(select(Invoice).where(Invoice.id == str(invoice["id"])))
        ).scalar_one()
        assert float(stored.total) == BILLED_TOTAL
        assert float(stored.amount_paid) == BILLED_TOTAL
        assert stored.paid_at is not None


# --- operational visibility -------------------------------------------------


async def _dashboard(client: AsyncClient, **params) -> dict:
    return await _ok(await client.get(f"{REPORTS_URL}/dashboard", params=params or None))


class TestDashboardReflectsTheVisit:
    @pytest.mark.asyncio
    async def test_the_dashboard_shows_the_work_as_it_happens(
        self,
        db: AsyncSession,
        manager_client: AsyncClient,
        technician_client: AsyncClient,
        parts_client: AsyncClient,
        customer_client: AsyncClient,
        portal_customer: Customer,
        staff: dict[str, str],
    ):
        """The dashboard is read between the steps, not after them.

        A dashboard that is correct at the end of the day can be wrong all day
        and nobody would know: the screen exists to answer "what needs me now",
        and the only moment that question is interesting is before the work is
        finished. So this drives the same visit and snapshots the numbers at each
        handover -- in the shop, on hold, awaiting inspection, billed, overdue,
        paid.
        """
        seen: dict[str, dict] = {}

        async def observe(stage_name: str, facts: dict) -> None:
            seen[stage_name] = await _dashboard(manager_client)
            if stage_name != "issued":
                return
            # The bill has just gone out, and this is the moment it would still be
            # chased. The promised payment date is moved into the past here, in
            # the middle of the visit, because overdue is only observable while
            # the invoice is still open -- a test that backdated it afterwards
            # would be asserting against a settled bill, which is not overdue by
            # definition. Written to the row rather than through the API because a
            # date on an issued invoice is not the shop's to change once the
            # customer holds the document; a shop that genuinely had to move one
            # would void it and raise a new one, and that would be a different
            # invoice in the log.
            issued_row = (
                await db.execute(
                    select(Invoice).where(Invoice.id == str(facts["invoice"]["id"]))
                )
            ).scalar_one()
            issued_row.due_date = date.today() - timedelta(days=10)
            await db.commit()
            seen["issued_overdue"] = await _dashboard(manager_client)

        await run_the_whole_visit(
            advisor=manager_client,
            technician=technician_client,
            parts_clerk=parts_client,
            customer=customer_client,
            record=portal_customer,
            staff=staff,
            observe=observe,
        )

        # An empty shop on an empty morning: numbers, not errors.
        fresh = seen["requested"]
        assert fresh["revenue"]["collected_total"] == 0.0
        assert fresh["work"]["repair_orders_open"] == 0
        assert fresh["operations"]["vehicles_in_shop"] == 0

        # The car is booked in and on the order, so it is in the shop and the
        # order is open but not yet finished.
        in_shop = seen["started"]
        assert in_shop["operations"]["vehicles_in_shop"] == 1
        assert in_shop["work"]["repair_orders_open"] == 1
        assert in_shop["work"]["repair_orders_in_progress"] == 1
        assert in_shop["work"]["repair_orders_awaiting_qc"] == 0
        # Nothing is waiting on the parts department yet.
        assert in_shop["work"]["part_requests_pending"] == 0

        # A part request exists and nobody has answered it. This is the queue that
        # lives in one person's head unless a dashboard counts it, and it is
        # counted on both the work panel and the operations panel because both
        # panels are read by the people who have to chase it.
        waiting_on_parts = seen["part_requested"]
        assert waiting_on_parts["work"]["part_requests_pending"] == 1
        assert waiting_on_parts["operations"]["pending_part_requests"] == 1

        staged = seen["part_staged"]
        assert staged["work"]["part_requests_pending"] == 0
        assert staged["operations"]["pending_part_requests"] == 0

        # Work finished, not yet inspected. This is the queue a shop forgets, and
        # the moment the dashboard exists for.
        waiting = seen["awaiting_qc"]
        assert waiting["work"]["repair_orders_awaiting_qc"] == 1
        assert waiting["work"]["repair_orders_open"] == 0
        assert waiting["work"]["completed_in_period"] == 1
        assert waiting["revenue"]["collected_total"] == 0.0
        assert waiting["revenue"]["outstanding_balance"] == 0.0

        # Inspected and released: the order has left every queue, and no money has
        # moved yet. Billing is not taking.
        released = seen["qc_passed"]
        assert released["work"]["repair_orders_awaiting_qc"] == 0
        assert released["work"]["repair_orders_open"] == 0
        assert released["revenue"]["collected_total"] == 0.0
        assert released["revenue"]["outstanding_balance"] == 0.0

        # Billed but unpaid: invoiced money is not collected money. Merging the
        # two is the single easiest way for a shop to look richer than it is.
        issued = seen["issued"]
        assert issued["revenue"]["invoiced_total"] == BILLED_TOTAL
        assert issued["revenue"]["collected_total"] == 0.0
        assert issued["revenue"]["outstanding_balance"] == BILLED_TOTAL
        assert issued["revenue"]["overdue_count"] == 0
        assert issued["money"]["invoices_issued"] == 1
        assert issued["money"]["invoices_paid"] == 0

        # The promised payment date is now in the past and the bill is still
        # open. This is the figure the owner actually acts on -- which bill to
        # chase first -- and it is the one number a report is most likely to lose
        # by applying the reporting window to it.
        overdue = seen["issued_overdue"]
        assert overdue["revenue"]["overdue_count"] == 1
        assert overdue["revenue"]["overdue_balance"] == BILLED_TOTAL
        # Still outstanding, and still counted: overdue is a worse kind of open,
        # not a different kind of closed.
        assert overdue["revenue"]["outstanding_balance"] == BILLED_TOTAL
        assert overdue["revenue"]["invoiced_total"] == BILLED_TOTAL

        # Paid: the money is in and the overdue flag clears, because a settled
        # bill is not chased.
        settled = seen["paid"]
        assert settled["revenue"]["collected_total"] == BILLED_TOTAL
        assert settled["revenue"]["outstanding_balance"] == 0.0
        assert settled["revenue"]["overdue_count"] == 0
        assert settled["money"]["invoices_paid"] == 1
        assert settled["money"]["invoices_issued"] == 0
        assert settled["money"]["invoices_partially_paid"] == 0

    @pytest.mark.asyncio
    async def test_the_period_governs_the_money_and_never_the_queues(
        self,
        db: AsyncSession,
        manager_client: AsyncClient,
        technician_client: AsyncClient,
        parts_client: AsyncClient,
        customer_client: AsyncClient,
        portal_customer: Customer,
        staff: dict[str, str],
    ):
        """A window filters money; it never filters what is waiting on you.

        Backdating the bill by six weeks puts the invoicing outside a thirty-day
        window while leaving the payment inside it, which is the worst possible
        arrangement for a dashboard that applies one filter to everything: the
        money goes missing and the debt stays. The balance and the overdue count
        are facts about today and must survive any window, because the chaseable
        debt is the oldest debt.
        """
        facts = await run_the_whole_visit(
            advisor=manager_client,
            technician=technician_client,
            parts_clerk=parts_client,
            customer=customer_client,
            record=portal_customer,
            staff=staff,
        )

        invoice = facts["invoice"]
        stored = (
            await db.execute(select(Invoice).where(Invoice.id == str(invoice["id"])))
        ).scalar_one()
        stored.invoice_date = date.today() - timedelta(days=40)
        stored.due_date = date.today() - timedelta(days=30)
        await db.commit()

        thirty = await _dashboard(manager_client)
        # The bill was raised six weeks ago, so it is not this month's invoicing...
        assert thirty["revenue"]["invoiced_total"] == 0.0
        # ...but the customer paid today, and takings are takings.
        assert thirty["revenue"]["collected_total"] == BILLED_TOTAL
        # What they still owe, and how long it has been owing, are not flows and
        # do not move when the window does.
        assert thirty["revenue"]["outstanding_balance"] == 0.0
        assert thirty["revenue"]["overdue_count"] == 0

        ninety = await _dashboard(
            manager_client,
            start_date=str(date.today() - timedelta(days=89)),
            end_date=str(date.today()),
        )
        assert ninety["revenue"]["invoiced_total"] == BILLED_TOTAL
        assert ninety["revenue"]["collected_total"] == BILLED_TOTAL

        # A window that ended before any of this happened. This is the control:
        # it proves the previous two windows were not "identical" merely because
        # every field is unfiltered.
        yesterday_date = date.today() - timedelta(days=1)
        yesterday = await _dashboard(
            manager_client,
            start_date=str(yesterday_date),
            end_date=str(yesterday_date),
        )

        # The queue figures are identical in every window, because a queue is a question
        # about today. The car is still booked in and the order is still closed;
        # no reporting period makes either of them leave the shop.
        for window in (thirty, ninety, yesterday):
            assert window["work"]["repair_orders_open"] == 0
            assert window["work"]["repair_orders_awaiting_qc"] == 0
            assert window["work"]["part_requests_pending"] == 0
            assert window["operations"]["vehicles_in_shop"] == 1
            # The status counts are of rows, not of flows, so they are not
            # windowed either.
            assert window["money"]["invoices_paid"] == 1
            # And with nothing owed there is nothing to chase, in any window.
            assert window["revenue"]["outstanding_balance"] == 0.0
            assert window["revenue"]["overdue_count"] == 0

        # Only the flow counts move with the window. The car was finished today,
        # so today and the last ninety days both count the completion and the
        # payment...
        assert thirty["work"]["completed_in_period"] == 1
        assert ninety["work"]["completed_in_period"] == 1
        # ...while a window that closed yesterday reports neither, and correctly
        # reports no takings for a payment it cannot see.
        assert yesterday["work"]["completed_in_period"] == 0
        assert yesterday["revenue"]["collected_total"] == 0.0
        assert yesterday["revenue"]["invoiced_total"] == 0.0
