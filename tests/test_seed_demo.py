# ruff: noqa: DTZ011
"""Tests for the demo data seed.

The point of these tests is not that the seed *runs* — that is one line — but that
what it produces is **coherent**. A demo database with an invoice whose total does
not match its lines, or a repair order belonging to a vehicle that belongs to
somebody else, teaches a developer the wrong thing about the system and makes
every report built on it a lie.

So these tests check referential integrity, that the money adds up, that the stock
ledger reconciles, and that the dataset is the shape the reports were written for:
every invoice state present, every report with something to say. The portal
account is checked through the portal's own endpoints, because an empty portal is
the demo defect nobody notices until somebody opens the browser, and the
documented logins are checked against the script that documents them.

The whole seed runs inside a savepoint and is rolled back afterwards, because the
test database is shared by the whole suite and a shop's worth of demo rows left
behind would break every counting test that runs after this file. The two tests
that sign in are the exception: signing in commits, so they use the logins from
the RBAC seed alone and let the ``db`` fixture clean up after them.
"""

from __future__ import annotations

import ast
import itertools
import re

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.appointments.models import Appointment
from app.core.demo_data import (
    DEMO_ADVISOR_EMAIL,
    DEMO_CUSTOMER_EMAIL,
    DEMO_OWNER_EMAIL,
    DEMO_TECH_EMAIL,
    seed_demo_data,
)
from app.customers.models import Customer
from app.estimates.models import Estimate, EstimateItem
from app.inventory.models import InventoryTransaction
from app.invoices.models import Invoice, InvoiceItem
from app.notifications.models import Notification
from app.parts.models import Part
from app.payments.models import Payment
from app.repair_orders.models import RepairOrder
from app.vehicles.models import Vehicle


@pytest.fixture()
async def seeded(db: AsyncSession):
    """Seed inside a savepoint, then undo it.

    A rollback rather than a delete: the rows reference the demo users created
    by the session fixture, and unwinding the transaction is the only way to be
    sure nothing survives.
    """
    savepoint = await db.begin_nested()
    result = await seed_demo_data(db)
    yield result
    await savepoint.rollback()


# --- the seed itself ---------------------------------------------------------


class TestSeedDemoData:
    async def test_it_writes_every_domain(self, seeded, db: AsyncSession):
        assert seeded.created
        counts = seeded.counts
        assert counts["customers"] == 6
        assert counts["vehicles"] == 8
        assert counts["suppliers"] == 3
        assert counts["parts"] == 10
        assert counts["estimates"] == 4
        assert counts["repair_orders"] == 8
        assert counts["invoices"] == 7
        assert counts["notifications"] > 0
        # One opening receipt per part, plus the units fitted to the seeded jobs.
        assert counts["inventory_transactions"] > counts["parts"]

    async def test_running_it_twice_does_not_double_anything(
        self, seeded, db: AsyncSession
    ):
        """Every developer runs the seed script twice. It has to notice.

        The guard is the portal-linked customer, so this is really asserting that
        the idempotency check is looking at a row that genuinely exists rather
        than at something optional.
        """
        before = await _count(db, Customer)
        second = await seed_demo_data(db)
        assert second.created is False
        assert await _count(db, Customer) == before

    async def test_exactly_one_customer_is_linked_to_a_login(
        self, seeded, db: AsyncSession
    ):
        """The portal derives the customer from the token.

        Two demo customers with logins would give the portal a choice to make,
        and a portal that picks the wrong one shows somebody their neighbour's
        invoices.
        """
        linked = await _count(db, Customer, Customer.user_id.isnot(None))
        assert linked == 1

        portal_customer = (
            await db.execute(
                select(Customer).where(Customer.email == DEMO_CUSTOMER_EMAIL)
            )
        ).scalar_one()
        assert portal_customer.user_id is not None

    async def test_every_vehicle_belongs_to_a_real_customer(
        self, seeded, db: AsyncSession
    ):
        customer_ids = set((await db.execute(select(Customer.id))).scalars())
        vehicle_ids = set((await db.execute(select(Vehicle.id))).scalars())
        for vehicle in (await db.execute(select(Vehicle))).scalars():
            assert vehicle.customer_id in customer_ids
            assert vehicle.id in vehicle_ids
            assert vehicle.vin and len(vehicle.vin) == 17

    async def test_every_repair_order_matches_its_vehicle_owner(
        self, seeded, db: AsyncSession
    ):
        """An RO that belongs to a different customer's car is the classic
        demo-data lie, and the one that makes service history look broken.
        """
        owners = {
            v.id: v.customer_id
            for v in (await db.execute(select(Vehicle))).scalars()
        }
        for order in (await db.execute(select(RepairOrder))).scalars():
            assert owners[order.vehicle_id] == order.customer_id


# --- the money ---------------------------------------------------------------


class TestDemoMoney:
    async def test_estimate_totals_match_their_lines(
        self, seeded, db: AsyncSession
    ):
        """A stored total that disagrees with the lines that produced it is how a
        report ends up confidently wrong.
        """
        for estimate in (await db.execute(select(Estimate))).scalars():
            items = list(
                (
                    await db.execute(
                        select(EstimateItem).where(
                            EstimateItem.estimate_id == estimate.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            charges = sum(
                i.line_total
                for i in items
                if i.item_type != "DISCOUNT"
            )
            assert estimate.subtotal == pytest.approx(charges, abs=0.01)
            assert estimate.total == pytest.approx(
                estimate.subtotal - estimate.discount_amount + estimate.tax_amount,
                abs=0.01,
            )
            assert estimate.total >= 0

    async def test_invoice_totals_match_their_lines(
        self, seeded, db: AsyncSession
    ):
        for invoice in (await db.execute(select(Invoice))).scalars():
            items = list(
                (
                    await db.execute(
                        select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id)
                    )
                )
                .scalars()
                .all()
            )
            charges = sum(i.line_total for i in items if i.item_type != "DISCOUNT")
            assert invoice.subtotal == pytest.approx(charges, abs=0.01)
            assert invoice.total == pytest.approx(
                invoice.subtotal - invoice.discount_amount + invoice.tax_amount,
                abs=0.01,
            )

    async def test_amount_paid_agrees_with_the_payments(
        self, seeded, db: AsyncSession
    ):
        """``amount_paid`` is the header; the payments are the truth.

        If they disagree the "billed versus collected" report has no correct
        answer, because the two halves are describing different shops.
        """
        for invoice in (await db.execute(select(Invoice))).scalars():
            paid = float(
                (
                    await db.execute(
                        select(func.coalesce(func.sum(Payment.amount), 0.0)).where(
                            Payment.invoice_id == invoice.id,
                            Payment.status == "RECORDED",
                        )
                    )
                ).scalar_one()
            )
            assert float(invoice.amount_paid) == pytest.approx(paid, abs=0.01)

    async def test_invoice_status_matches_what_was_paid(
        self, seeded, db: AsyncSession
    ):
        """Status, header and payments all have to tell the same story."""
        for invoice in (await db.execute(select(Invoice))).scalars():
            paid = float(invoice.amount_paid or 0.0)
            total = float(invoice.total or 0.0)
            if invoice.status == "PAID":
                assert paid == pytest.approx(total, abs=0.01)
            elif invoice.status == "PARTIALLY_PAID":
                assert 0 < paid < total
            else:
                assert paid == 0.0

    async def test_nothing_is_billed_past_its_total(
        self, seeded, db: AsyncSession
    ):
        """The database enforces this; asserting it here says why it is there.

        An overpayment is a credit for the customer, not a negative bill, and the
        constraint is the only thing stopping a payment route from producing one.
        """
        for invoice in (await db.execute(select(Invoice))).scalars():
            assert float(invoice.amount_paid or 0.0) <= float(invoice.total or 0.0) + 0.01


# --- the ledger ---------------------------------------------------------------


class TestDemoStockIsOnTheLedger:
    """``quantity_on_hand`` is a running total, so every unit has to be on it.

    A demo database whose parts carry a balance and no transactions would
    contradict the rule the inventory module is built on, and would leave the
    ledger's own question — "how many brake pads do we have, and why" —
    unanswerable on the very data a developer explores first.
    """

    async def test_every_unit_on_the_shelf_arrived_on_the_ledger(
        self, seeded, db: AsyncSession
    ):
        parts = (await db.execute(select(Part))).scalars().all()
        assert parts, "the seed is supposed to stock parts"
        for part in parts:
            movements = (
                (
                    await db.execute(
                        select(InventoryTransaction).where(
                            InventoryTransaction.part_id == part.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert movements, f"{part.part_number} has stock and no history"
            balance = sum(float(m.quantity) for m in movements)
            assert balance == pytest.approx(float(part.quantity_on_hand), abs=0.01)

    async def test_each_row_states_the_balance_either_side_of_itself(
        self, seeded, db: AsyncSession
    ):
        """The rows are ordered, and each one starts where the last one finished.

        This is the property that makes a single row explain a movement on its
        own, months after the fact.
        """
        movements = (
            (
                await db.execute(
                    select(InventoryTransaction).order_by(InventoryTransaction.created_at)
                )
            )
            .scalars()
            .all()
        )
        assert movements
        by_part: dict[object, list[InventoryTransaction]] = {}
        for movement in movements:
            by_part.setdefault(movement.part_id, []).append(movement)

        for rows in by_part.values():
            assert float(rows[0].quantity_before) == pytest.approx(0.0, abs=0.01)
            for previous, current in itertools.pairwise(rows):
                assert float(current.quantity_before) == pytest.approx(
                    float(previous.quantity_after), abs=0.01
                )
            for row in rows:
                assert float(row.quantity_before) + float(row.quantity) == pytest.approx(
                    float(row.quantity_after), abs=0.01
                )
                assert float(row.quantity_after) >= 0.0

    async def test_stock_fitted_to_a_job_names_that_job(
        self, seeded, db: AsyncSession
    ):
        """An ISSUE that does not say which order took it cannot be costed.

        The whole reason the ledger records ``repair_order_id`` is that a part on
        a job and a part on the shelf are different money.
        """
        order_ids = set((await db.execute(select(RepairOrder.id))).scalars())
        issues = (
            (
                await db.execute(
                    select(InventoryTransaction).where(
                        InventoryTransaction.transaction_type == "ISSUE"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert issues, "parts are fitted to work, and the demo should show it"
        for issue in issues:
            assert issue.repair_order_id is not None
            assert issue.repair_order_id in order_ids
            assert issue.reference  # the RO number, so a human can find it
            assert float(issue.quantity) < 0

    async def test_low_stock_alerts_have_stock_to_alert_on(
        self, seeded, db: AsyncSession
    ):
        """Low stock is derived, and the demo has to actually trip it.

        Every part seeded with a quantity at or below its reorder point is what
        makes the alert list and the reorder-from-low-stock builder return
        something rather than an empty table.
        """
        low = [
            p
            for p in (await db.execute(select(Part))).scalars()
            if float(p.quantity_on_hand) <= float(p.reorder_level)
        ]
        assert low, "a demo shop with nothing to reorder is not a useful demo"


# --- the attention centres ----------------------------------------------------


class TestDemoNotifications:
    async def test_the_badges_are_not_all_zero(
        self, seeded, db: AsyncSession
    ):
        """A demo whose every notification list is empty looks broken, not clean.

        Staff are first-class recipients in this system, so the shop's own people
        are seeded with something waiting on them as well as the customer.
        """
        notifications = (await db.execute(select(Notification))).scalars().all()
        assert notifications
        recipients = {n.recipient_id for n in notifications}
        assert len(recipients) >= 3, "only one person has anything to look at"
        unread = [n for n in notifications if n.read_at is None]
        assert unread, "every notice already read means every badge reads zero"

    async def test_notices_reach_the_account_behind_the_customer(
        self, seeded, db: AsyncSession
    ):
        """The portal customer gets one; a customer with no login gets none.

        The four demo customers who never made an account are the quiet-drop case
        the delivery service is built to handle, and a seed that invented
        recipients for them would hide it.
        """
        portal_user_id = (
            await db.execute(
                select(Customer.user_id).where(Customer.email == DEMO_CUSTOMER_EMAIL)
            )
        ).scalar_one()
        mine = (
            (
                await db.execute(
                    select(Notification).where(
                        Notification.recipient_id == portal_user_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert mine
        assert any(n.notification_type == "INVOICE_ISSUED" for n in mine)

    async def test_a_notice_about_an_invoice_comes_from_that_invoice(
        self, seeded, db: AsyncSession
    ):
        """``entity_type`` is a loose pointer, so the seed has to make it true.

        A deep link that resolves to nothing is worse than no deep link: the user
        clicks through and lands on a 404 with no way to tell it from a bug.
        """
        invoice_ids = set((await db.execute(select(Invoice.id))).scalars())
        appointment_ids = set((await db.execute(select(Appointment.id))).scalars())
        order_ids = set((await db.execute(select(RepairOrder.id))).scalars())
        part_ids = set((await db.execute(select(Part.id))).scalars())
        resolvable = {
            "invoice": invoice_ids,
            "appointment": appointment_ids,
            "repair_order": order_ids,
            "part": part_ids,
        }
        notifications = (await db.execute(select(Notification))).scalars().all()
        assert notifications
        for notification in notifications:
            known = resolvable.get(notification.entity_type or "")
            assert known is not None, f"unknown entity_type {notification.entity_type!r}"
            assert notification.entity_id in known

    async def test_a_low_stock_notice_names_the_part(
        self, seeded, db: AsyncSession
    ):
        """The text is what a person reads, so it has to name the right thing.

        A notice that said "BRAKES is low on stock" when the brake pads were the
        problem would send somebody to the wrong shelf, and the catalog has both a
        category and a name to get wrong.
        """
        parts = {
            p.id: p.name
            for p in (await db.execute(select(Part))).scalars()
        }
        notices = (
            (
                await db.execute(
                    select(Notification).where(
                        Notification.notification_type == "INVENTORY_LOW"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert notices, "there is stock below its reorder point, so say so"
        for notice in notices:
            assert notice.entity_id in parts
            assert parts[notice.entity_id] in notice.title

    async def test_no_notice_is_stacked_twice(
        self, seeded, db: AsyncSession
    ):
        """The dedupe guarantee, asserted on the seed's own rows.

        Every seeded notice carries a key, and the database's partial unique index
        is what stops a re-run or a second sweep from doubling them.
        """
        keys = [
            n.dedupe_key
            for n in (await db.execute(select(Notification))).scalars()
            if n.dedupe_key
        ]
        assert keys, "seeded notices should be idempotent"
        assert len(keys) == len(set(keys))


# --- the demo account is worth signing into ----------------------------------


class TestThePortalAccountHasSomethingToDo:
    """``customer@autofix.demo`` is the account the demo documentation points at.

    A login that shows an empty account is worse than no login: it reads as a
    broken portal rather than a quiet shop. So the demo customer is given an
    estimate awaiting a decision, a bill they have not paid, an appointment
    request in flight and a settled bill behind them — and these are asserted
    through the portal's own endpoints, because that is what somebody signing in
    will actually see.
    """

    async def test_the_summary_lists_their_cars_and_balance(
        self, seeded, customer_client
    ):
        response = await customer_client.get("/api/v1/portal/")
        assert response.status_code == 200
        body = response.json()
        assert body["vehicle_count"] == 2
        assert float(body["open_balance"]) > 0.0
        assert float(body["overdue_balance"]) == 0.0
        assert body["awaiting_approval"] == 1
        assert body["awaiting_payment"] == 1
        assert body["ready_for_pickup"] == 0
        assert body["next_appointment"] is not None

    async def test_an_estimate_is_waiting_on_them(
        self, seeded, customer_client
    ):
        response = await customer_client.get(
            "/api/v1/portal/estimates", params={"open_only": True}
        )
        assert response.status_code == 200
        open_estimates = response.json()
        assert len(open_estimates) == 1
        assert open_estimates[0]["status"] == "SENT"

    async def test_they_owe_money_and_have_paid_money(
        self, seeded, customer_client
    ):
        response = await customer_client.get("/api/v1/portal/invoices")
        assert response.status_code == 200
        statuses = sorted(i["status"] for i in response.json())
        assert statuses == ["ISSUED", "PAID"]

        unpaid = await customer_client.get(
            "/api/v1/portal/invoices", params={"unpaid_only": True}
        )
        assert len(unpaid.json()) == 1

    async def test_an_appointment_request_is_in_flight(
        self, seeded, customer_client
    ):
        response = await customer_client.get("/api/v1/portal/appointments")
        assert response.status_code == 200
        assert any(a["status"] == "REQUESTED" for a in response.json())

    async def test_they_cannot_see_anybody_elses_car(
        self, seeded, customer_client, db: AsyncSession
    ):
        """The isolation rule, exercised against seeded data rather than fixtures.

        The portal resolves the customer from the token, so this is the test that
        would fail if somebody ever added a customer id parameter.
        """
        linked_customer_ids = {
            c.id
            for c in (
                await db.execute(
                    select(Customer).where(Customer.user_id.isnot(None))
                )
            )
            .scalars()
            .all()
        }
        assert linked_customer_ids
        vehicles = (await db.execute(select(Vehicle))).scalars().all()
        theirs = {v.license_plate for v in vehicles if v.customer_id in linked_customer_ids}
        everyone = {v.license_plate for v in vehicles}
        assert len(theirs) == 2
        assert everyone - theirs, "the seed needs other customers to hide"

        response = await customer_client.get("/api/v1/portal/vehicles")
        assert {v["license_plate"] for v in response.json()} == theirs


# --- the shape the reports need ---------------------------------------------


class TestDemoDataIsReportable:
    async def test_every_invoice_state_is_present(
        self, seeded, db: AsyncSession
    ):
        """Draft, issued, part-paid and paid all appear.

        A seed where everything is settled makes the revenue report's
        billed-versus-collected distinction invisible, which is the one thing
        that report exists to show.
        """
        statuses = {
            i.status
            for i in (await db.execute(select(Invoice))).scalars()
        }
        assert {"DRAFT", "ISSUED", "PARTIALLY_PAID", "PAID"} <= statuses

    async def test_an_overdue_bill_is_actually_overdue(
        self, seeded, db: AsyncSession
    ):
        """Unpaid and past its due date, not merely unpaid.

        The chase list is the report that gets used on a Monday morning, and a
        "receivables" figure made of invoices that are not yet due is a
        collections list with nothing on it.
        """
        from datetime import date

        today = date.today()
        overdue = [
            i
            for i in (await db.execute(select(Invoice))).scalars()
            if i.status == "ISSUED" and i.due_date is not None and i.due_date < today
        ]
        assert len(overdue) == 1
        assert float(overdue[0].amount_paid or 0.0) == 0.0

    async def test_orders_exist_in_several_states(
        self, seeded, db: AsyncSession
    ):
        statuses = {o.status for o in (await db.execute(select(RepairOrder))).scalars()}
        assert {"DRAFT", "IN_PROGRESS", "COMPLETED", "DELIVERED"} <= statuses

    async def test_somebody_is_a_repeat_customer(
        self, seeded, db: AsyncSession
    ):
        """Retention counts *settled* bills, so it needs one customer with two.

        A demo where exactly one person has ever paid a bill reports a zero repeat
        rate — which is the "healthy shop full of one-timers" the report exists to
        contradict, and a number a reader would believe.
        """
        repeats = (
            await db.execute(
                select(Invoice.customer_id, func.count(Invoice.id))
                .where(Invoice.status == "PAID")
                .group_by(Invoice.customer_id)
                .having(func.count(Invoice.id) > 1)
            )
        ).all()
        assert repeats, "the retention report has nobody to find"

    async def test_technicians_can_be_ranked(
        self, seeded, db: AsyncSession
    ):
        """The productivity report groups labour by the person who did it.

        Labour with no technician attached is invisible in that report, so
        seeding unattributed hours would quietly halve the numbers.
        """
        from app.labor.models import LaborRecord

        total, attributed = (
            await db.execute(
                select(func.count(LaborRecord.id), func.count(LaborRecord.technician_id))
            )
        ).one()
        assert total > 0
        assert attributed == total

    async def test_the_demo_people_are_who_the_data_says_they_are(
        self, seeded, db: AsyncSession
    ):
        """Staff are attached to the work, not floating.

        A repair order with no advisor is a report that cannot answer "who
        promised it", which is the question a customer complaint starts with.
        """
        orders = (await db.execute(select(RepairOrder))).scalars().all()
        assert all(o.advisor_id is not None for o in orders)
        assert any(o.technician_id is not None for o in orders)

        estimates = (await db.execute(select(Estimate))).scalars().all()
        assert all(e.created_by_id is not None for e in estimates)
        assert all(
            i.recorded_by_id is not None
            for i in (await db.execute(select(Payment))).scalars()
        )


# --- the documented accounts --------------------------------------------------


class TestTheDocumentedAccounts:
    """``scripts/seed.py`` is where a developer looks to find the logins.

    So the list in its docstring is held to the database, in both directions and
    by actually signing in — documentation that is confidently wrong about how to
    get into the demo is the one kind of demo defect no test would otherwise
    catch.
    """

    async def test_the_documented_emails_are_the_seeded_ones(
        self, seeded, db: AsyncSession
    ):
        from app.auth.models import User

        for email in (
            DEMO_OWNER_EMAIL,
            DEMO_ADVISOR_EMAIL,
            DEMO_TECH_EMAIL,
            DEMO_CUSTOMER_EMAIL,
        ):
            found = (
                await db.execute(select(User).where(User.email == email))
            ).scalar_one_or_none()
            assert found is not None, f"{email} is documented but not seeded"

    async def test_every_demo_account_is_documented(self, db: AsyncSession):
        """The CLI's account list and the seeded logins must agree exactly.

        Checked in both directions. A documented account that does not exist is
        the obvious failure; an account that exists and is not documented is the
        one that bites instead, because it works and nobody knows the password is
        also on the login screen.
        """
        from app.auth.models import User

        documented = set(_documented_accounts())
        seeded = {
            row[0]
            for row in (
                await db.execute(
                    select(User.email).where(User.email.like("%@autofix.demo"))
                )
            ).all()
        }
        assert documented, "the seed script no longer documents any accounts"
        assert seeded == documented

    async def test_each_documented_account_can_actually_sign_in(self, client):
        """Every documented login works, with the documented password.

        A seed that creates a user without a usable password passes every other
        test in the suite and leaves a developer locked out of the demo on their
        first run. Deliberately *not* seeded with demo data: signing in commits,
        which would end the savepoint the rest of this file's fixture relies on,
        and the logins come from the RBAC seed the ``db`` fixture always runs.
        """
        from app.core.seed import TEST_PASSWORD

        assert TEST_PASSWORD in _script_docstring(), (
            "the CLI no longer states the demo password it seeds"
        )
        documented = sorted(_documented_accounts())
        assert len(documented) >= 5
        for email in documented:
            response = await client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": TEST_PASSWORD},
            )
            assert response.status_code == 200, f"{email} cannot sign in"
            assert response.json()["access_token"]


def _script_docstring() -> str:
    """The seed script's own documentation, read from the file rather than
    retyped here.

    Importing the module would execute nothing harmful, but reading the source is
    what makes this a check on the documentation a developer actually reads.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "scripts" / "seed.py"
    return ast.get_docstring(ast.parse(source.read_text(encoding="utf-8"))) or ""


def _documented_accounts() -> list[str]:
    """Read the demo account list out of that docstring."""
    return re.findall(r"([\w.+-]+@autofix\.demo)", _script_docstring())


# --- helpers -----------------------------------------------------------------


async def _count(db: AsyncSession, model, *where) -> int:
    stmt = select(func.count(model.id))
    if where:
        stmt = stmt.where(*where)
    return int((await db.execute(stmt)).scalar_one() or 0)
