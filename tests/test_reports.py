# ruff: noqa: DTZ011
# Reports compare against the shop's local calendar day, which is exactly what
# `date.today()` returns; a UTC date here would disagree with the API.
"""Tests for reports and analytics.

The reports are read-only projections, so the bulk of these tests are about
*definitions* rather than CRUD: which statuses count as revenue, what happens to
an empty division, whether an unpaid bill from outside the window still shows as
money owed, and whether a caller can ask for a report at all.

Two structural properties are asserted directly rather than by implication: the
router exposes no write method, and the module registers no models — both are
the reason a report cannot start disagreeing with the ledger it reads.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.customers.models import Customer
from app.inventory.schemas import InventoryTransactionCreate
from app.inventory.services import InventoryService
from app.invoices.models import Invoice, InvoiceStatus
from app.notifications.models import Notification
from app.parts.models import Part, PartStatus
from app.parts.schemas import PartCreate, PartUpdate
from app.parts.services import PartService
from app.reports.services import ReportService
from app.vehicles.models import Vehicle
from tests.factories import VehicleFactory

REPORTS_URL = "/api/v1/reports"
INVOICES_URL = "/api/v1/invoices/"
PAYMENTS_URL = "/api/v1/payments/"
RO_URL = "/api/v1/repair_orders/"
LABOR_URL = "/api/v1/labor/"

# A cartesian product in a reporting query still returns a number, and the number
# is quietly wrong. SQLAlchemy warns about it, so these tests treat the warning as
# the failure it is rather than letting it scroll past.
pytestmark = pytest.mark.filterwarnings("error::sqlalchemy.exc.SAWarning")


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- fixtures --------------------------------------------------------------


@pytest.fixture()
async def customer(db: AsyncSession) -> Customer:
    record = Customer(
        first_name="Report",
        last_name="Customer",
        email="reports_test@example.com",
        phone="555-0400",
        preferred_contact="EMAIL",
        customer_status="ACTIVE",
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


@pytest.fixture()
async def vehicle(db: AsyncSession, customer: Customer) -> Vehicle:
    record = VehicleFactory.build(customer_id=customer.id)
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


@pytest.fixture()
async def second_customer(db: AsyncSession) -> Customer:
    record = Customer(
        first_name="Second",
        last_name="Customer",
        email="reports_second@example.com",
        preferred_contact="EMAIL",
        customer_status="ACTIVE",
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


# --- helpers ---------------------------------------------------------------


async def _set_ro_status(client: AsyncClient, ro_id: str, status: str) -> dict:
    response = await client.patch(f"{RO_URL}{ro_id}/status", params={"status": status})
    assert response.status_code == 200, response.text
    return response.json()


async def create_billable_ro(
    client: AsyncClient,
    customer: Customer,
    vehicle: Vehicle,
    *,
    labor_for: User | None = None,
    hours: tuple[float, float, float] = (2.0, 2.0, 80.0),
) -> dict:
    """Drive a repair order to QC_PASSED, which is the earliest it can be billed.

    Labor can only be logged while the order is actually being worked on, so the
    optional ``labor_for`` records it on the way past IN_PROGRESS rather than
    after the fact.
    """
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
    if labor_for is not None:
        assigned = await client.patch(
            f"{RO_URL}{ro['id']}", json={"technician_id": str(labor_for.id)}
        )
        assert assigned.status_code == 200, assigned.text
    await _set_ro_status(client, ro["id"], "APPROVED")
    await _set_ro_status(client, ro["id"], "IN_PROGRESS")
    if labor_for is not None:
        await _log_labor(client, ro["id"], labor_for, actual=hours[0], billable=hours[1], rate=hours[2])
    for task in ro["tasks"]:
        done = await client.patch(
            f"{RO_URL}{ro['id']}/tasks/{task['id']}/status",
            params={"status": "COMPLETED"},
        )
        assert done.status_code == 200, done.text
    await _set_ro_status(client, ro["id"], "COMPLETED")
    return await _set_ro_status(client, ro["id"], "QC_PASSED")


async def create_ro_stopped_at(
    client: AsyncClient, customer: Customer, vehicle: Vehicle, stop_at: str
) -> dict:
    """A repair order parked at ``stop_at``, for the queue figures."""
    created = await client.post(
        RO_URL,
        json={
            "customer_id": str(customer.id),
            "vehicle_id": str(vehicle.id),
            "tasks": [{"description": "Waiting job"}],
        },
    )
    assert created.status_code == 201, created.text
    ro = created.json()
    if stop_at == "DRAFT":
        return ro

    await _set_ro_status(client, ro["id"], "APPROVED")
    if stop_at == "APPROVED":
        return ro

    await _set_ro_status(client, ro["id"], "IN_PROGRESS")
    if stop_at == "ON_HOLD":
        # Still waiting on a part: the order is parked mid-job.
        return await _set_ro_status(client, ro["id"], "ON_HOLD")

    for task in ro["tasks"]:
        done = await client.patch(
            f"{RO_URL}{ro['id']}/tasks/{task['id']}/status",
            params={"status": "COMPLETED"},
        )
        assert done.status_code == 200, done.text
    return await _set_ro_status(client, ro["id"], stop_at)


async def create_invoice(
    client: AsyncClient,
    customer: Customer,
    vehicle: Vehicle,
    total: float,
    *,
    issue: bool = True,
    **overrides,
) -> dict:
    """Raise an invoice for ``total`` against fresh finished work."""
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
    if not issue:
        return invoice
    issued = await client.post(f"{INVOICES_URL}{invoice['id']}/issue")
    assert issued.status_code == 200, issued.text
    return issued.json()


async def pay(client: AsyncClient, invoice_id: str, amount: float, **overrides) -> dict:
    response = await client.post(
        PAYMENTS_URL, json={"invoice_id": invoice_id, "amount": amount, **overrides}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def set_invoice_status(db: AsyncSession, invoice_id: str, status: str) -> None:
    """Force a status the API refuses to set directly (VOID, for instance)."""
    result = await db.execute(select(Invoice).where(Invoice.id == str(invoice_id)))
    invoice = result.scalar_one()
    invoice.status = status
    await db.commit()


async def create_part(
    db: AsyncSession,
    *,
    part_number: str,
    name: str,
    category: str = "BRAKES",
    unit_cost: float = 10.0,
    unit_price: float = 25.0,
    reorder_level: float = 2.0,
    status: str = PartStatus.ACTIVE.value,
    stock: float = 0.0,
) -> Part:
    service = PartService(db)
    part = await service.create_part(
        PartCreate(
            part_number=part_number,
            name=name,
            category=category,
            unit_cost=unit_cost,
            unit_price=unit_price,
            reorder_level=reorder_level,
        )
    )
    if stock:
        await InventoryService(db).record_transaction(
            InventoryTransactionCreate(
                part_id=part.id,
                transaction_type="RECEIPT",
                quantity=stock,
                unit_cost=unit_cost,
            )
        )
    if status != PartStatus.ACTIVE.value:
        part = await service.update_part(part.id, PartUpdate(status=status))
    return part


async def get_report(path: str, client: AsyncClient, **params) -> dict:
    response = await client.get(f"{REPORTS_URL}{path}", params=params or None)
    assert response.status_code == 200, response.text
    return response.json()


async def _make_tech(db: AsyncSession, email: str, first: str, last: str) -> User:
    """A technician account the productivity report can attribute work to."""
    from app.core.security import hash_password

    user = User(
        email=email,
        password_hash=hash_password("demo1234"),
        first_name=first,
        last_name=last,
        is_active=True,
        is_staff=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _log_labor(
    client: AsyncClient,
    ro_id: str,
    tech: User,
    *,
    actual: float,
    billable: float,
    rate: float,
) -> dict:
    response = await client.post(
        LABOR_URL,
        json={
            "repair_order_id": ro_id,
            "technician_id": str(tech.id),
            "description": "Brake service",
            "actual_hours": actual,
            "billable_hours": billable,
            "hourly_rate": rate,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- revenue ---------------------------------------------------------------


class TestRevenueReport:
    @pytest.mark.asyncio
    async def test_empty_shop_reports_zeroes_not_errors(
        self, owner_client: AsyncClient
    ):
        """A garage on its first morning must get numbers, not a stack trace."""
        report = await get_report("/revenue", owner_client)

        assert report["invoiced_total"] == 0.0
        assert report["invoice_count"] == 0
        assert report["average_invoice"] == 0.0
        assert report["collected_total"] == 0.0
        assert report["outstanding_balance"] == 0.0
        assert report["series"]

    @pytest.mark.asyncio
    async def test_default_period_is_the_last_thirty_days(self, owner_client: AsyncClient):
        report = await get_report("/revenue", owner_client)

        assert report["period"]["days"] == 30
        assert report["period"]["start_date"] == str(date.today() - timedelta(days=29))
        assert report["period"]["end_date"] == str(date.today())
        assert report["period"]["granularity"] == "DAY"

    @pytest.mark.asyncio
    async def test_issued_invoice_counts_as_invoiced(
        self,
        owner_client: AsyncClient,
        customer: Customer,
        vehicle: Vehicle,
    ):
        await create_invoice(owner_client, customer, vehicle, 400.0)

        report = await get_report("/revenue", owner_client)
        assert report["invoiced_total"] == 400.0
        assert report["invoice_count"] == 1
        assert report["average_invoice"] == 400.0

    @pytest.mark.asyncio
    async def test_draft_is_not_revenue_but_is_still_visible(
        self,
        owner_client: AsyncClient,
        customer: Customer,
        vehicle: Vehicle,
    ):
        """A draft has not been sent. It must not inflate revenue, and it must not
        vanish either — the manager asking why the month looks short is owed the
        drafts and write-offs holding the number back."""
        await create_invoice(owner_client, customer, vehicle, 250.0, issue=False)

        report = await get_report("/revenue", owner_client)
        assert report["invoiced_total"] == 0.0
        assert report["invoiced_by_status"]["DRAFT"] == 250.0

    @pytest.mark.asyncio
    async def test_void_invoice_is_excluded_from_revenue(
        self,
        owner_client: AsyncClient,
        db: AsyncSession,
        customer: Customer,
        vehicle: Vehicle,
    ):
        invoice = await create_invoice(owner_client, customer, vehicle, 300.0)
        await set_invoice_status(db, invoice["id"], InvoiceStatus.VOID.value)

        report = await get_report("/revenue", owner_client)
        assert report["invoiced_total"] == 0.0
        assert report["invoiced_by_status"]["VOID"] == 300.0

    @pytest.mark.asyncio
    async def test_payments_are_collected_separately_from_billing(
        self,
        owner_client: AsyncClient,
        customer: Customer,
        vehicle: Vehicle,
    ):
        """Money owed is not money received. The two figures must never be merged."""
        invoice = await create_invoice(owner_client, customer, vehicle, 500.0)
        await pay(owner_client, invoice["id"], 200.0)

        report = await get_report("/revenue", owner_client)
        assert report["invoiced_total"] == 500.0
        assert report["collected_total"] == 200.0
        assert report["payment_count"] == 1
        assert report["outstanding_balance"] == 300.0

    @pytest.mark.asyncio
    async def test_voided_payment_is_subtracted_from_takings(
        self,
        owner_client: AsyncClient,
        customer: Customer,
        vehicle: Vehicle,
    ):
        invoice = await create_invoice(owner_client, customer, vehicle, 180.0)
        payment = await pay(owner_client, invoice["id"], 180.0)
        voided = await owner_client.post(
            f"{PAYMENTS_URL}{payment['id']}/void", json={"reason": "wrong customer"}
        )
        assert voided.status_code == 200, voided.text

        report = await get_report("/revenue", owner_client)
        assert report["collected_total"] == 0.0
        assert report["voided_total"] == 180.0
        assert report["void_count"] == 1
        assert report["outstanding_balance"] == 180.0

    @pytest.mark.asyncio
    async def test_outstanding_ignores_the_period_window(
        self,
        owner_client: AsyncClient,
        db: AsyncSession,
        customer: Customer,
        vehicle: Vehicle,
    ):
        """A balance is not a flow. An old unpaid bill is still owed today, and
        scoping it to a 30-day window would hide the most chaseable debt."""
        invoice = await create_invoice(owner_client, customer, vehicle, 900.0)
        await pay(owner_client, invoice["id"], 100.0)

        result = await db.execute(select(Invoice).where(Invoice.id == str(invoice["id"])))
        stored = result.scalar_one()
        stored.invoice_date = date.today() - timedelta(days=400)
        stored.due_date = date.today() - timedelta(days=370)
        await db.commit()

        report = await get_report(
            "/revenue",
            owner_client,
            start_date=str(date.today() - timedelta(days=29)),
            end_date=str(date.today()),
        )
        assert report["invoiced_total"] == 0.0
        assert report["outstanding_balance"] == 800.0
        assert report["outstanding_count"] == 1
        assert report["overdue_balance"] == 800.0
        assert report["overdue_count"] == 1

    @pytest.mark.asyncio
    async def test_settled_invoice_leaves_nothing_outstanding(
        self,
        owner_client: AsyncClient,
        customer: Customer,
        vehicle: Vehicle,
    ):
        invoice = await create_invoice(owner_client, customer, vehicle, 75.0)
        await pay(owner_client, invoice["id"], 75.0)

        report = await get_report("/revenue", owner_client)
        assert report["outstanding_balance"] == 0.0
        assert report["outstanding_count"] == 0
        assert report["overdue_count"] == 0

    @pytest.mark.asyncio
    async def test_series_has_one_point_per_day_with_no_gaps(
        self,
        owner_client: AsyncClient,
        customer: Customer,
        vehicle: Vehicle,
    ):
        """A quiet day has to appear as a zero. A missing point draws as a slope
        across the gap and shows revenue that never happened."""
        await create_invoice(owner_client, customer, vehicle, 120.0)

        report = await get_report(
            "/revenue",
            owner_client,
            start_date=str(date.today() - timedelta(days=6)),
            end_date=str(date.today()),
        )
        assert len(report["series"]) == 7
        assert report["series"][-1]["invoiced"] == 120.0
        assert report["series"][0]["invoiced"] == 0.0
        assert report["series"][0]["collected"] == 0.0

    @pytest.mark.asyncio
    async def test_long_window_switches_to_monthly_buckets(
        self, owner_client: AsyncClient
    ):
        report = await get_report(
            "/revenue",
            owner_client,
            start_date=str(date.today() - timedelta(days=400)),
            end_date=str(date.today()),
        )

        assert report["period"]["granularity"] == "MONTH"
        assert 12 <= len(report["series"]) <= 20
        assert all(
            date.fromisoformat(point["period_start"]).day == 1
            for point in report["series"]
        )

    @pytest.mark.asyncio
    async def test_reversed_window_is_refused(self, owner_client: AsyncClient):
        response = await owner_client.get(
            f"{REPORTS_URL}/revenue",
            params={
                "start_date": str(date.today()),
                "end_date": str(date.today() - timedelta(days=5)),
            },
        )

        assert response.status_code == 400
        assert "must not be after" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_revenue_requires_reports_read(
        self, customer_client: AsyncClient, owner_client: AsyncClient
    ):
        assert (await customer_client.get(f"{REPORTS_URL}/revenue")).status_code == 403
        assert (await owner_client.get(f"{REPORTS_URL}/revenue")).status_code == 200


# --- repair order analytics -------------------------------------------------


class TestRepairOrderReport:
    @pytest.mark.asyncio
    async def test_status_counts_are_a_live_snapshot(
        self, owner_client: AsyncClient, customer: Customer, vehicle: Vehicle
    ):
        """The queue is a question about today, not about March."""
        await create_billable_ro(owner_client, customer, vehicle)
        created = await owner_client.post(
            RO_URL,
            json={
                "customer_id": str(customer.id),
                "vehicle_id": str(vehicle.id),
                "tasks": [{"description": "Replace wipers"}],
            },
        )
        assert created.status_code == 201, created.text

        report = await get_report(
            "/repair-orders",
            owner_client,
            start_date=str(date.today() - timedelta(days=1)),
            end_date=str(date.today()),
        )
        assert report["status_counts"]["QC_PASSED"] == 1
        assert report["status_counts"]["DRAFT"] == 1
        assert sum(report["status_counts"].values()) == 2

    @pytest.mark.asyncio
    async def test_period_counts_follow_the_window(
        self, owner_client: AsyncClient, customer: Customer, vehicle: Vehicle
    ):
        await create_billable_ro(owner_client, customer, vehicle)

        inside = await get_report("/repair-orders", owner_client)
        assert inside["total_orders"] == 1
        assert inside["opened"] == 1
        assert inside["completed"] == 1
        assert inside["completion_rate"] == 1.0

        outside = await get_report(
            "/repair-orders",
            owner_client,
            start_date=str(date.today() - timedelta(days=10)),
            end_date=str(date.today() - timedelta(days=5)),
        )
        assert outside["total_orders"] == 0
        assert outside["completed"] == 0
        # Zero orders must not divide by zero in front of the owner.
        assert outside["completion_rate"] == 0.0
        assert outside["average_cycle_hours"] == 0.0

    @pytest.mark.asyncio
    async def test_cancelled_work_is_excluded_from_the_completion_rate(
        self, owner_client: AsyncClient, customer: Customer, vehicle: Vehicle
    ):
        """An abandoned job was never going to be finished. Counting it in the
        denominator would flatter the shop for work it declined to do."""
        cancelled = await owner_client.post(
            RO_URL,
            json={
                "customer_id": str(customer.id),
                "vehicle_id": str(vehicle.id),
                "tasks": [{"description": "Dropped job"}],
            },
        )
        assert cancelled.status_code == 201, cancelled.text
        await owner_client.patch(
            f"{RO_URL}{cancelled.json()['id']}/status",
            params={"status": "CANCELLED", "reason": "Customer declined"},
        )

        report = await get_report("/repair-orders", owner_client)
        assert report["cancelled"] == 1
        assert report["completed"] == 0
        assert report["completion_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_cycle_time_is_measured_start_to_finish(
        self, owner_client: AsyncClient, db: AsyncSession, customer: Customer, vehicle: Vehicle
    ):
        """Cycle time is real elapsed time, not the sum of logged hours."""
        from app.repair_orders.models import RepairOrder

        ro = await create_billable_ro(owner_client, customer, vehicle)
        result = await db.execute(select(RepairOrder).where(RepairOrder.id == ro["id"]))
        stored = result.scalar_one()
        stored.started_at = _utcnow() - timedelta(hours=8)
        stored.completed_at = _utcnow()
        await db.commit()

        report = await get_report("/repair-orders", owner_client)
        assert 7.5 <= report["average_cycle_hours"] <= 8.5

    @pytest.mark.asyncio
    async def test_series_is_zero_filled(
        self, owner_client: AsyncClient, customer: Customer, vehicle: Vehicle
    ):
        await create_billable_ro(owner_client, customer, vehicle)

        report = await get_report(
            "/repair-orders",
            owner_client,
            start_date=str(date.today() - timedelta(days=4)),
            end_date=str(date.today()),
        )
        assert len(report["series"]) == 5
        assert report["series"][0]["opened"] == 0
        assert report["series"][0]["completed"] == 0
        assert report["series"][-1]["completed"] == 1

    @pytest.mark.asyncio
    async def test_repair_orders_require_reports_read(
        self, customer_client: AsyncClient, technician_client: AsyncClient
    ):
        assert (await customer_client.get(f"{REPORTS_URL}/repair-orders")).status_code == 403
        assert (
            await technician_client.get(f"{REPORTS_URL}/repair-orders")
        ).status_code == 200


# --- technician productivity ----------------------------------------------


class TestTechnicianProductivity:
    @pytest.fixture()
    async def tech(self, db: AsyncSession) -> User:
        return await _make_tech(db, "reports_tech@example.com", "Pat", "Mechanic")

    @pytest.fixture()
    async def second_tech(self, db: AsyncSession) -> User:
        return await _make_tech(db, "reports_tech2@example.com", "Sam", "Wrench")

    @pytest.mark.asyncio
    async def test_labor_becomes_productivity(
        self,
        owner_client: AsyncClient,
        customer: Customer,
        vehicle: Vehicle,
        tech: User,
    ):
        await create_billable_ro(
            owner_client,
            customer,
            vehicle,
            labor_for=tech,
            hours=(3.5, 2.0, 80.0),
        )

        report = await get_report("/technicians", owner_client)
        assert report["totals"]["technician_count"] == 1

        row = report["technicians"][0]
        assert row["technician_id"] == str(tech.id)
        assert row["name"] == "Pat Mechanic"
        assert row["labor_hours_actual"] == 3.5
        assert row["labor_hours_billable"] == 2.0
        assert row["labor_revenue"] == 160.0
        assert row["repair_orders_completed"] == 1

    @pytest.mark.asyncio
    async def test_a_technician_with_no_work_in_the_period_is_absent(
        self, owner_client: AsyncClient, tech: User
    ):
        """An empty row for somebody who did nothing is noise on a report whose
        whole purpose is to say who did the work."""
        report = await get_report("/technicians", owner_client)
        assert report["technicians"] == []
        assert report["totals"]["technician_count"] == 0
        assert report["totals"]["labor_revenue"] == 0.0

    @pytest.mark.asyncio
    async def test_tasks_and_orders_are_counted_per_person(
        self,
        owner_client: AsyncClient,
        db: AsyncSession,
        customer: Customer,
        vehicle: Vehicle,
        tech: User,
    ):
        ro = await create_billable_ro(
            owner_client, customer, vehicle, labor_for=tech, hours=(1.0, 1.0, 50.0)
        )

        from app.repair_orders.models import RepairOrder

        result = await db.execute(select(RepairOrder).where(RepairOrder.id == ro["id"]))
        stored = result.scalar_one()
        stored.technician_id = tech.id
        stored.started_at = _utcnow() - timedelta(hours=5)
        stored.completed_at = _utcnow()
        stored.tasks[0].assigned_to_id = tech.id
        await db.commit()

        row = (await get_report("/technicians", owner_client))["technicians"][0]
        assert row["tasks_completed"] == 1
        assert row["repair_orders_assigned"] == 1
        assert row["repair_orders_completed"] == 1
        assert 4.0 <= row["average_cycle_hours"] <= 5.5

    @pytest.mark.asyncio
    async def test_busiest_technician_sorts_first(
        self,
        owner_client: AsyncClient,
        customer: Customer,
        vehicle: Vehicle,
        tech: User,
        second_tech: User,
    ):
        """A productivity report that does not rank is a list, and a list does not
        answer the question anybody opened it for."""
        await create_billable_ro(
            owner_client,
            customer,
            vehicle,
            labor_for=tech,
            hours=(1.0, 1.0, 10.0),
        )
        second_ro = await create_billable_ro(
            owner_client,
            customer,
            vehicle,
            labor_for=second_tech,
            hours=(9.0, 9.0, 100.0),
        )
        assert second_ro["id"] != ""

        report = await get_report("/technicians", owner_client)
        assert [row["labor_revenue"] for row in report["technicians"]] == [900.0, 10.0]
        assert report["totals"]["labor_revenue"] == 910.0
        assert report["totals"]["technician_count"] == 2
        assert report["totals"]["labor_hours_actual"] == 10.0

    @pytest.mark.asyncio
    async def test_analytics_is_closed_to_service_advisors(
        self, manager_client: AsyncClient, owner_client: AsyncClient
    ):
        """This report ranks named colleagues. That is management's to read, not
        something every member of staff should be handed."""
        assert (await manager_client.get(f"{REPORTS_URL}/technicians")).status_code == 403
        assert (await owner_client.get(f"{REPORTS_URL}/technicians")).status_code == 200

    @pytest.mark.asyncio
    async def test_technician_can_see_the_shop_wide_reports_but_not_this_one(
        self, technician_client: AsyncClient
    ):
        assert (await technician_client.get(f"{REPORTS_URL}/revenue")).status_code == 200
        assert (await technician_client.get(f"{REPORTS_URL}/technicians")).status_code == 403


# --- inventory value -------------------------------------------------------


class TestInventoryValueReport:
    @pytest.mark.asyncio
    async def test_stock_is_valued_at_cost_and_retail(
        self, owner_client: AsyncClient, db: AsyncSession
    ):
        await create_part(
            db,
            part_number="VAL-1",
            name="Brake pad",
            category="BRAKES",
            unit_cost=10.0,
            unit_price=30.0,
            stock=4.0,
        )

        report = await get_report("/inventory-value", owner_client)
        assert report["cost_value"] == 40.0
        assert report["retail_value"] == 120.0
        assert report["potential_margin"] == 80.0
        assert report["part_count"] == 1
        assert report["units_on_hand"] == 4.0

    @pytest.mark.asyncio
    async def test_value_groups_by_category(
        self, owner_client: AsyncClient, db: AsyncSession
    ):
        await create_part(
            db, part_number="CAT-1", name="Pad", category="BRAKES", stock=2.0
        )
        await create_part(
            db, part_number="CAT-2", name="Oil", category="LUBRICANTS", stock=5.0
        )

        report = await get_report("/inventory-value", owner_client)
        categories = {row["category"]: row for row in report["by_category"]}
        assert set(categories) == {"BRAKES", "LUBRICANTS"}
        assert categories["BRAKES"]["cost_value"] == 20.0
        assert categories["LUBRICANTS"]["units_on_hand"] == 5.0

    @pytest.mark.asyncio
    async def test_low_stock_is_listed_and_out_of_stock_is_flagged(
        self, owner_client: AsyncClient, db: AsyncSession
    ):
        await create_part(
            db, part_number="LOW-1", name="Low pad", reorder_level=4.0, stock=1.0
        )
        await create_part(
            db, part_number="OUT-1", name="Empty filter", reorder_level=2.0, stock=0.0
        )

        report = await get_report("/inventory-value", owner_client)
        flagged = {item["part_number"]: item for item in report["low_stock_items"]}

        assert flagged["LOW-1"]["stock_status"] == "LOW"
        assert flagged["OUT-1"]["stock_status"] == "OUT"
        assert report["low_stock_count"] == 2
        assert report["out_of_stock_count"] == 1

    @pytest.mark.asyncio
    async def test_discontinued_parts_are_not_reorder_alarms(
        self, owner_client: AsyncClient, db: AsyncSession
    ):
        """A part that is never going to be reordered should not appear on a list
        people read every morning; the false alarm trains them to skip it."""
        await create_part(
            db,
            part_number="DEAD-1",
            name="Obsolete pad",
            reorder_level=4.0,
            stock=0.0,
            status=PartStatus.DISCONTINUED.value,
        )

        report = await get_report("/inventory-value", owner_client)
        assert report["low_stock_items"] == []
        assert report["low_stock_count"] == 0
        # Still valued: the stock on the shelf is still worth something.
        assert report["part_count"] == 1

    @pytest.mark.asyncio
    async def test_top_movers_rank_consumed_stock_by_value(
        self, owner_client: AsyncClient, db: AsyncSession
    ):
        small = await create_part(
            db, part_number="MOV-1", name="Common pad", unit_cost=1.0, stock=50.0
        )
        big = await create_part(
            db, part_number="MOV-2", name="Expensive module", unit_cost=500.0, stock=10.0
        )
        inventory = InventoryService(db)
        await inventory.record_transaction(
            InventoryTransactionCreate(
                part_id=small.id, transaction_type="ISSUE", quantity=5.0, unit_cost=1.0
            )
        )
        await inventory.record_transaction(
            InventoryTransactionCreate(
                part_id=big.id, transaction_type="ISSUE", quantity=3.0, unit_cost=500.0
            )
        )

        report = await get_report("/inventory-value", owner_client)
        movers = report["top_movers"]
        assert [row["part_number"] for row in movers] == ["MOV-2", "MOV-1"]
        assert movers[0]["value_issued"] == 1500.0
        assert movers[0]["quantity_issued"] == 3.0
        assert movers[1]["value_issued"] == 5.0

    @pytest.mark.asyncio
    async def test_valuation_needs_inventory_access_not_merely_reports_access(
        self, customer_client: AsyncClient, parts_client: AsyncClient, owner_client: AsyncClient
    ):
        """It is a report about the books, and it is also the purchase price of
        every part on the shelf. The gate is ``inventory:read``, not
        ``reports:read`` — a customer who could read reports must not be able to
        read the shop's margin."""
        assert (
            await customer_client.get(f"{REPORTS_URL}/inventory-value")
        ).status_code == 403
        assert (
            await parts_client.get(f"{REPORTS_URL}/inventory-value")
        ).status_code == 200
        assert (await owner_client.get(f"{REPORTS_URL}/inventory-value")).status_code == 200


# --- customer retention ----------------------------------------------------


class TestCustomerRetentionReport:
    async def _paid_invoice(
        self,
        client: AsyncClient,
        customer: Customer,
        vehicle: Vehicle,
        total: float,
    ) -> dict:
        invoice = await create_invoice(client, customer, vehicle, total)
        await pay(client, invoice["id"], total)
        return invoice

    @pytest.mark.asyncio
    async def test_only_settled_bills_count(
        self,
        owner_client: AsyncClient,
        customer: Customer,
        vehicle: Vehicle,
    ):
        await create_invoice(owner_client, customer, vehicle, 400.0)

        report = await get_report("/customer-retention", owner_client)
        assert report["customers_billed"] == 0
        assert report["settled_revenue"] == 0.0
        assert report["top_customers"] == []

    @pytest.mark.asyncio
    async def test_a_first_time_customer_is_new_not_returning(
        self,
        owner_client: AsyncClient,
        customer: Customer,
        vehicle: Vehicle,
    ):
        await self._paid_invoice(owner_client, customer, vehicle, 100.0)

        report = await get_report("/customer-retention", owner_client)
        assert report["customers_billed"] == 1
        assert report["new_customers"] == 1
        assert report["returning_customers"] == 0
        assert report["repeat_rate"] == 0.0
        assert report["settled_revenue"] == 100.0
        assert report["revenue_per_customer"] == 100.0

    @pytest.mark.asyncio
    async def test_a_customer_who_comes_back_is_counted_as_retained(
        self,
        owner_client: AsyncClient,
        customer: Customer,
        vehicle: Vehicle,
    ):
        await self._paid_invoice(owner_client, customer, vehicle, 100.0)
        await self._paid_invoice(owner_client, customer, vehicle, 150.0)

        report = await get_report("/customer-retention", owner_client)
        assert report["customers_billed"] == 1
        assert report["new_customers"] == 1
        assert report["returning_customers"] == 1
        assert report["repeat_rate"] == 1.0
        assert report["settled_revenue"] == 250.0

    @pytest.mark.asyncio
    async def test_retention_looks_back_beyond_the_window(
        self,
        owner_client: AsyncClient,
        db: AsyncSession,
        customer: Customer,
        vehicle: Vehicle,
    ):
        """A customer who returned after a year away is exactly who this report
        exists to find. Counting only inside the window would report a shop full
        of one-timers and make them stop reading it."""
        first = await self._paid_invoice(owner_client, customer, vehicle, 200.0)
        await self._paid_invoice(owner_client, customer, vehicle, 300.0)

        result = await db.execute(select(Invoice).where(Invoice.id == str(first["id"])))
        old = result.scalar_one()
        old.invoice_date = date.today() - timedelta(days=500)
        await db.commit()

        report = await get_report(
            "/customer-retention",
            owner_client,
            start_date=str(date.today() - timedelta(days=29)),
            end_date=str(date.today()),
        )
        assert report["customers_billed"] == 1
        assert report["returning_customers"] == 1
        assert report["settled_revenue"] == 300.0
        # The first-ever visit was long before this window, so this is a
        # returning customer, not a new one.
        assert report["new_customers"] == 0

    @pytest.mark.asyncio
    async def test_top_customers_are_ranked_and_limited(
        self,
        owner_client: AsyncClient,
        db: AsyncSession,
        customer: Customer,
        second_customer: Customer,
        vehicle: Vehicle,
    ):
        second_vehicle = VehicleFactory.build(customer_id=second_customer.id)
        db.add(second_vehicle)
        await db.commit()
        await db.refresh(second_vehicle)

        await self._paid_invoice(owner_client, customer, vehicle, 100.0)
        await self._paid_invoice(owner_client, second_customer, second_vehicle, 900.0)

        report = await get_report("/customer-retention", owner_client, limit=1)
        assert len(report["top_customers"]) == 1
        assert report["top_customers"][0]["revenue"] == 900.0
        assert report["top_customers"][0]["name"] == "Second Customer"
        # The limit caps the list, never the totals.
        assert report["settled_revenue"] == 1000.0
        assert report["revenue_per_customer"] == 500.0

    @pytest.mark.asyncio
    async def test_retention_is_closed_to_service_advisors(
        self, manager_client: AsyncClient, owner_client: AsyncClient
    ):
        assert (
            await manager_client.get(f"{REPORTS_URL}/customer-retention")
        ).status_code == 403
        assert (
            await owner_client.get(f"{REPORTS_URL}/customer-retention")
        ).status_code == 200


# --- dashboard -------------------------------------------------------------


class TestDashboard:
    @pytest.mark.asyncio
    async def test_dashboard_assembles_every_section(
        self,
        owner_client: AsyncClient,
        customer: Customer,
        vehicle: Vehicle,
    ):
        invoice = await create_invoice(owner_client, customer, vehicle, 450.0)
        await pay(owner_client, invoice["id"], 150.0)
        # A second job finished but not yet inspected — the queue a shop forgets.
        await create_ro_stopped_at(owner_client, customer, vehicle, "COMPLETED")

        dashboard = await get_report("/dashboard", owner_client)

        assert dashboard["revenue"]["invoiced_total"] == 450.0
        assert dashboard["revenue"]["collected_total"] == 150.0
        assert dashboard["revenue"]["outstanding_balance"] == 300.0
        assert dashboard["work"]["repair_orders_awaiting_qc"] == 1
        assert dashboard["work"]["repair_orders_open"] == 0
        assert dashboard["work"]["completed_in_period"] == 2
        assert dashboard["money"]["invoices_partially_paid"] == 1
        assert dashboard["operations"]["unread_notifications"] == 0
        assert dashboard["generated_for"]

    @pytest.mark.asyncio
    async def test_dashboard_separates_the_three_work_queues(
        self, owner_client: AsyncClient, customer: Customer, vehicle: Vehicle
    ):
        """Approved, in progress, on hold and awaiting QC are four different
        problems, and collapsing them into "open" would hide the one that is
        actually stuck."""
        await create_ro_stopped_at(owner_client, customer, vehicle, "APPROVED")
        await create_ro_stopped_at(owner_client, customer, vehicle, "ON_HOLD")

        work = (await get_report("/dashboard", owner_client))["work"]
        assert work["repair_orders_open"] == 2
        assert work["repair_orders_on_hold"] == 1
        assert work["repair_orders_awaiting_qc"] == 0

    @pytest.mark.asyncio
    async def test_dashboard_counts_the_callers_own_unread_notifications(
        self, owner_client: AsyncClient, db: AsyncSession
    ):
        """The badge is about *your* attention, so it is counted for the signed-in
        user and nobody else."""
        from app.auth.services import AuthService

        owner = await AuthService(db).get_user_by_email("owner@autofix.demo")
        advisor = await AuthService(db).get_user_by_email("manager@autofix.demo")
        db.add_all(
            [
                Notification(
                    recipient_id=owner.id,
                    notification_type="ESTIMATE_READY",
                    title="Estimate ready",
                ),
                Notification(
                    recipient_id=advisor.id,
                    notification_type="ESTIMATE_READY",
                    title="Estimate ready",
                ),
            ]
        )
        await db.commit()

        dashboard = await get_report("/dashboard", owner_client)
        assert dashboard["operations"]["unread_notifications"] == 1

    @pytest.mark.asyncio
    async def test_dashboard_is_denied_to_customers(
        self, customer_client: AsyncClient, manager_client: AsyncClient
    ):
        assert (await customer_client.get(f"{REPORTS_URL}/dashboard")).status_code == 403
        assert (await manager_client.get(f"{REPORTS_URL}/dashboard")).status_code == 200

    @pytest.mark.asyncio
    async def test_dashboard_respects_the_period(self, owner_client: AsyncClient):
        dashboard = await get_report(
            "/dashboard",
            owner_client,
            start_date=str(date.today() - timedelta(days=6)),
            end_date=str(date.today()),
        )
        assert dashboard["period"]["days"] == 7

        refused = await owner_client.get(
            f"{REPORTS_URL}/dashboard",
            params={
                "start_date": str(date.today()),
                "end_date": str(date.today() - timedelta(days=1)),
            },
        )
        assert refused.status_code == 400


# --- structure -------------------------------------------------------------


class TestReportModuleShape:
    """The properties that make a report a report rather than a stored number."""

    def test_the_router_exposes_no_write_method(self):
        """No POST, PUT or DELETE anywhere. The moment a report can be edited, it
        stops being a report of what happened."""
        from app.reports.routes import router

        methods = {
            method
            for route in router.routes
            for method in getattr(route, "methods", set())
        }
        assert methods == {"GET"}

    def test_the_module_defines_no_tables(self):
        """A cached summary row is a second source of truth, and this project has
        been bitten by that before: a total copied onto a repair order, a balance
        cached beside the payments that produce it."""
        from app.core.database import Base

        owned = sorted(
            table.name
            for table in Base.metadata.tables.values()
            for cls in table.__class__.__mro__
            if cls.__module__.startswith("app.reports")
        )
        assert owned == []

    @pytest.mark.asyncio
    async def test_service_agrees_with_the_api(
        self, db: AsyncSession, owner_client: AsyncClient, customer: Customer, vehicle: Vehicle
    ):
        """The dashboard is assembled from the same queries as the reports, so the
        two cannot drift apart. That is only true if they share the code path."""
        await create_invoice(owner_client, customer, vehicle, 320.0)

        service = ReportService(db)
        revenue = await service.revenue_report()
        api = await get_report("/revenue", owner_client)

        assert revenue.invoiced_total == api["invoiced_total"]
        assert revenue.invoice_count == api["invoice_count"]

    @pytest.mark.asyncio
    async def test_granularity_falls_back_to_months_for_a_long_window(
        self, db: AsyncSession
    ):
        service = ReportService(db)
        period = service.resolve_period(
            date.today() - timedelta(days=200), date.today()
        )
        assert period.granularity == "MONTH"
        assert period.days == 201

    @pytest.mark.asyncio
    async def test_reversed_period_raises_a_business_rule_error(self, db: AsyncSession):
        from app.common.exceptions import BusinessRuleError

        service = ReportService(db)
        with pytest.raises(BusinessRuleError):
            service.resolve_period(date.today(), date.today() - timedelta(days=1))
