"""Report and analytics queries.

Everything in this module is a **projection**. Nothing is written, and nothing is
cached: every figure is summed from the tables that own it at the moment the
report is asked for. That is a deliberate constraint rather than an omission —
a stored report row is a second source of truth, and this project's whole
history is a series of bugs caused by exactly that (a total copied onto a repair
order, a balance cached beside the payments that produce it, a stock level that
could be written directly instead of through the ledger).

Three rules run through the queries below.

**Money is counted by what happened, not by what it was called.** Invoiced money
comes from invoices that were *issued*; collected money comes from payments that
were *recorded*. Drafts are not revenue, voids are not revenue, and reversed
payments never touch the collected figure. Mixing those up is how a garage
convinces itself it earned money nobody paid.

**Averages are reported with their denominator.** A mean cycle time of 6 hours
means something entirely different over four jobs than over four hundred, and a
report that shows the mean alone is asking to be believed.

**Rates are guarded at zero.** Every ratio in here divides by a count that can
legitimately be zero — a quiet week with no finished jobs — and a division by
zero in a report endpoint is a 500 in front of the owner on a Monday morning.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import Date, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.appointments.models import ACTIVE_IN_SHOP_STATUSES, Appointment
from app.common.dates import today
from app.common.exceptions import BusinessRuleError
from app.customers.models import Customer
from app.inventory.models import InventoryTransaction, InventoryTransactionType
from app.invoices.models import (
    PAYABLE_INVOICE_STATUSES,
    Invoice,
    InvoiceStatus,
    round_money,
)
from app.labor.models import LaborRecord
from app.notifications.models import Notification
from app.part_requests.models import PartRequest, PartRequestStatus
from app.parts.models import Part, PartStatus
from app.payments.models import Payment, PaymentStatus
from app.repair_orders.models import (
    RepairOrder,
    RepairOrderStatus,
    RepairTask,
    RepairTaskStatus,
)
from app.reports.schemas import (
    DEFAULT_REPORT_DAYS,
    DEFAULT_TOP_LIMIT,
    GRANULARITY_DAY,
    GRANULARITY_MONTH,
    MAX_DAY_GRANULARITY_DAYS,
    CustomerRetentionReport,
    CustomerRevenueRow,
    DashboardMoney,
    DashboardOperations,
    DashboardReport,
    DashboardRevenue,
    DashboardWork,
    InventoryCategoryValue,
    InventoryValueReport,
    LowStockItem,
    RepairOrderReport,
    RepairOrderSeriesPoint,
    ReportPeriod,
    RevenuePoint,
    RevenueReport,
    StockMover,
    TechnicianProductivity,
    TechnicianProductivityReport,
    TechnicianTotals,
)

logger = logging.getLogger("autofix.reports.services")

# Invoice statuses that represent money the shop actually asked for. A DRAFT has
# not been sent and a VOID has been written off; neither is revenue, and counting
# them is the single easiest way to make a report wrong in the owner's favour.
INVOICED_STATUSES: frozenset[str] = frozenset(
    {
        InvoiceStatus.ISSUED.value,
        InvoiceStatus.PARTIALLY_PAID.value,
        InvoiceStatus.PAID.value,
    }
)

SECONDS_PER_HOUR = 3600.0


def _day_start(value: date) -> datetime:
    """Midnight UTC on the given day, for comparing against a timestamp column."""
    return datetime.combine(value, time.min, tzinfo=UTC)


def _day_end(value: date) -> datetime:
    """The last representable instant of the given day."""
    return datetime.combine(value, time.max, tzinfo=UTC)


def _add_month(value: date) -> date:
    """First day of the month after ``value``'s month, without a date library."""
    return date(value.year + (value.month // 12), (value.month % 12) + 1, 1)


def _bucket_starts(start: date, end: date, granularity: str) -> list[date]:
    """Every bucket start in the window, with **no gaps**.

    A period with no revenue has to appear as a zero, not be missing. A chart
    drawn from a sparse list silently closes the gap and shows a slope that never
    happened, so the series is built out from the calendar rather than out from
    the rows.
    """
    starts: list[date] = []
    if granularity == GRANULARITY_DAY:
        cursor = start
        while cursor <= end:
            starts.append(cursor)
            cursor += timedelta(days=1)
        return starts

    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        starts.append(cursor)
        cursor = _add_month(cursor)
    return starts


class ReportService:
    """Read-only reporting across the shop's ledger tables."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # --- period handling ----------------------------------------------------

    def resolve_period(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> ReportPeriod:
        """Work out the window a report covers, and how finely to slice it.

        The window defaults to the last 30 days rather than to all time: an
        open-ended query over an append-only ledger gets slower every year, and
        "since the shop opened" is not a question anybody actually asks.

        Granularity is chosen from the span instead of being demanded. Asking for
        a daily point per day over five years produces a thousand buckets that
        are almost all zero, which is not information and does cost a sort.
        """
        end = end_date or today()
        start = start_date or (end - timedelta(days=DEFAULT_REPORT_DAYS - 1))

        if start > end:
            raise BusinessRuleError(
                f"start_date ({start}) must not be after end_date ({end})"
            )

        span_days = (end - start).days + 1
        granularity = (
            GRANULARITY_DAY if span_days <= MAX_DAY_GRANULARITY_DAYS else GRANULARITY_MONTH
        )
        return ReportPeriod(
            start_date=start,
            end_date=end,
            days=span_days,
            granularity=granularity,
        )

    # --- revenue ------------------------------------------------------------

    async def revenue_report(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> RevenueReport:
        """What the shop charged and what it took, over a period."""
        period = self.resolve_period(start_date, end_date)
        start, end = period.start_date, period.end_date

        invoiced_stmt = select(
            func.coalesce(func.sum(Invoice.total), 0.0),
            func.count(Invoice.id),
        ).where(
            Invoice.status.in_(INVOICED_STATUSES),
            Invoice.invoice_date >= start,
            Invoice.invoice_date <= end,
        )
        invoiced_total, invoice_count = (await self.db.execute(invoiced_stmt)).one()

        # The per-status split is deliberately *not* filtered to the invoiced
        # statuses. A manager asking why the month looks short deserves to see
        # the drafts and the write-offs that are holding the number back.
        by_status_stmt = (
            select(Invoice.status, func.coalesce(func.sum(Invoice.total), 0.0))
            .where(Invoice.invoice_date >= start, Invoice.invoice_date <= end)
            .group_by(Invoice.status)
        )
        invoiced_by_status = {
            status: round_money(float(total))
            for status, total in (await self.db.execute(by_status_stmt)).all()
        }

        collected_stmt = select(
            func.coalesce(func.sum(Payment.amount), 0.0),
            func.count(Payment.id),
        ).where(
            Payment.status == PaymentStatus.RECORDED.value,
            Payment.payment_date >= start,
            Payment.payment_date <= end,
        )
        collected_total, payment_count = (await self.db.execute(collected_stmt)).one()

        voided_stmt = select(
            func.coalesce(func.sum(Payment.amount), 0.0),
            func.count(Payment.id),
        ).where(
            Payment.status == PaymentStatus.VOID.value,
            Payment.payment_date >= start,
            Payment.payment_date <= end,
        )
        voided_total, void_count = (await self.db.execute(voided_stmt)).one()

        outstanding = await self._outstanding()

        invoiced_total = round_money(float(invoiced_total or 0.0))
        series = await self._revenue_series(period)

        return RevenueReport(
            period=period,
            invoiced_total=invoiced_total,
            invoice_count=int(invoice_count or 0),
            average_invoice=round_money(
                invoiced_total / int(invoice_count) if invoice_count else 0.0
            ),
            collected_total=round_money(float(collected_total or 0.0)),
            payment_count=int(payment_count or 0),
            voided_total=round_money(float(voided_total or 0.0)),
            void_count=int(void_count or 0),
            outstanding_balance=outstanding["balance"],
            outstanding_count=outstanding["count"],
            overdue_balance=outstanding["overdue_balance"],
            overdue_count=outstanding["overdue_count"],
            invoiced_by_status=invoiced_by_status,
            series=series,
        )

    async def _outstanding(self) -> dict[str, float]:
        """Money the shop is owed right now, and how much of it is late.

        Deliberately **not** filtered by the report's period. This is a balance,
        not a flow: an invoice from eleven months ago that nobody has paid is
        still owed today, and scoping it to a 30-day window would quietly hide
        exactly the oldest — and most chaseable — debts on the books.
        """
        rows = (
            await self.db.execute(
                select(
                    Invoice.total,
                    Invoice.amount_paid,
                    Invoice.due_date,
                ).where(Invoice.status.in_(PAYABLE_INVOICE_STATUSES))
            )
        ).all()

        balance = 0.0
        overdue_balance = 0.0
        count = 0
        overdue_count = 0
        cutoff = today()
        for total, amount_paid, due_date in rows:
            outstanding = round_money(max(float(total or 0.0) - float(amount_paid or 0.0), 0.0))
            if outstanding <= 0:
                continue
            count += 1
            balance = round_money(balance + outstanding)
            if due_date is not None and due_date < cutoff:
                overdue_count += 1
                overdue_balance = round_money(overdue_balance + outstanding)

        return {
            "balance": balance,
            "count": count,
            "overdue_balance": overdue_balance,
            "overdue_count": overdue_count,
        }

    async def _revenue_series(self, period: ReportPeriod) -> list[RevenuePoint]:
        """Invoiced and collected per bucket, with empty buckets filled in."""
        unit = "day" if period.granularity == GRANULARITY_DAY else "month"
        invoiced_bucket = func.date_trunc(unit, Invoice.invoice_date).cast(Date).label("bucket")
        invoiced_stmt = (
            select(
                invoiced_bucket,
                func.coalesce(func.sum(Invoice.total), 0.0),
            )
            .where(
                Invoice.status.in_(INVOICED_STATUSES),
                Invoice.invoice_date >= period.start_date,
                Invoice.invoice_date <= period.end_date,
            )
            .group_by(invoiced_bucket)
        )
        collected_bucket = (
            func.date_trunc(unit, Payment.payment_date).cast(Date).label("bucket")
        )
        collected_stmt = (
            select(
                collected_bucket,
                func.coalesce(func.sum(Payment.amount), 0.0),
            )
            .where(
                Payment.status == PaymentStatus.RECORDED.value,
                Payment.payment_date >= period.start_date,
                Payment.payment_date <= period.end_date,
            )
            .group_by(collected_bucket)
        )

        invoiced = {
            bucket: round_money(float(total))
            for bucket, total in (await self.db.execute(invoiced_stmt)).all()
            if bucket is not None
        }
        collected = {
            bucket: round_money(float(total))
            for bucket, total in (await self.db.execute(collected_stmt)).all()
            if bucket is not None
        }

        return [
            RevenuePoint(
                period_start=bucket,
                invoiced=invoiced.get(bucket, 0.0),
                collected=collected.get(bucket, 0.0),
            )
            for bucket in _bucket_starts(
                period.start_date, period.end_date, period.granularity
            )
        ]

    # --- repair order analytics --------------------------------------------

    async def repair_order_report(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> RepairOrderReport:
        """How work moved through the shop over a period."""
        period = self.resolve_period(start_date, end_date)
        start, end = period.start_date, period.end_date
        window_start, window_end = _day_start(start), _day_end(end)

        status_rows = await self.db.execute(
            select(RepairOrder.status, func.count(RepairOrder.id)).group_by(
                RepairOrder.status
            )
        )
        status_counts = {status: int(count) for status, count in status_rows.all()}

        total_orders = int(
            (
                await self.db.execute(
                    select(func.count(RepairOrder.id)).where(
                        RepairOrder.created_at >= window_start,
                        RepairOrder.created_at <= window_end,
                    )
                )
            ).scalar_one()
            or 0
        )
        opened = await self._count_in_window(RepairOrder.started_at, window_start, window_end)
        completed = await self._count_in_window(
            RepairOrder.completed_at, window_start, window_end
        )
        delivered = await self._count_in_window(
            RepairOrder.delivered_at, window_start, window_end
        )
        cancelled = int(
            (
                await self.db.execute(
                    select(func.count(RepairOrder.id)).where(
                        RepairOrder.status == RepairOrderStatus.CANCELLED.value,
                        RepairOrder.created_at >= window_start,
                        RepairOrder.created_at <= window_end,
                    )
                )
            ).scalar_one()
            or 0
        )

        cycle_column = func.extract(
            "epoch", RepairOrder.completed_at - RepairOrder.started_at
        ).label("cycle")
        # The outer query must reference the *subquery's* column, not the labelled
        # expression: a labelled column still carries its table, so summing it
        # directly would drag `repair_orders` into the outer FROM as well and
        # quietly produce a cartesian product.
        cycle_source = (
            select(cycle_column)
            .where(
                RepairOrder.completed_at.isnot(None),
                RepairOrder.started_at.isnot(None),
                RepairOrder.completed_at >= window_start,
                RepairOrder.completed_at <= window_end,
            )
            .subquery()
        )
        cycle_seconds = (
            await self.db.execute(
                select(
                    func.coalesce(func.sum(cycle_source.c.cycle), 0.0),
                    func.count(),
                ).select_from(cycle_source)
            )
        ).one()
        cycle_total, cycle_count = cycle_seconds

        # Cancelled orders are subtracted from the denominator as well as the
        # numerator: an abandoned job was never going to be finished, so leaving
        # it out of both keeps the rate a statement about work the shop accepted.
        finishable = max(total_orders - cancelled, 0)
        completion_rate = (completed / finishable) if finishable else 0.0

        return RepairOrderReport(
            period=period,
            status_counts=status_counts,
            total_orders=total_orders,
            opened=opened,
            completed=completed,
            delivered=delivered,
            cancelled=cancelled,
            completion_rate=round(max(completion_rate, 0.0), 4),
            average_cycle_hours=(
                round(float(cycle_total or 0.0) / float(cycle_count) / SECONDS_PER_HOUR, 2)
                if cycle_count
                else 0.0
            ),
            series=await self._repair_order_series(period, window_start, window_end),
        )

    async def _count_in_window(
        self, column, window_start: datetime, window_end: datetime
    ) -> int:
        """Count non-null timestamps of a column inside the window.

        ``count(column)`` rather than ``count(*)`` with an ``IS NOT NULL``: COUNT
        ignores NULLs itself, so the same query answers both halves of the
        question instead of needing a subquery to do it.
        """
        return int(
            (
                await self.db.execute(
                    select(func.count(column)).where(
                        column >= window_start,
                        column <= window_end,
                    )
                )
            ).scalar_one()
            or 0
        )

    async def _repair_order_series(
        self, period: ReportPeriod, window_start: datetime, window_end: datetime
    ) -> list[RepairOrderSeriesPoint]:
        """Opened and completed per bucket, gaps filled with zeroes."""
        unit = "day" if period.granularity == GRANULARITY_DAY else "month"
        opened_bucket = func.date_trunc(unit, RepairOrder.started_at).cast(Date).label("bucket")
        opened_stmt = (
            select(
                opened_bucket,
                func.count(RepairOrder.id),
            )
            .where(
                RepairOrder.started_at >= window_start,
                RepairOrder.started_at <= window_end,
            )
            .group_by(opened_bucket)
        )
        completed_bucket = (
            func.date_trunc(unit, RepairOrder.completed_at).cast(Date).label("bucket")
        )
        completed_stmt = (
            select(
                completed_bucket,
                func.count(RepairOrder.id),
            )
            .where(
                RepairOrder.completed_at >= window_start,
                RepairOrder.completed_at <= window_end,
            )
            .group_by(completed_bucket)
        )

        opened = {
            bucket: int(count)
            for bucket, count in (await self.db.execute(opened_stmt)).all()
            if bucket is not None
        }
        completed = {
            bucket: int(count)
            for bucket, count in (await self.db.execute(completed_stmt)).all()
            if bucket is not None
        }

        return [
            RepairOrderSeriesPoint(
                period_start=bucket,
                opened=opened.get(bucket, 0),
                completed=completed.get(bucket, 0),
            )
            for bucket in _bucket_starts(
                period.start_date, period.end_date, period.granularity
            )
        ]

    # --- technician productivity -------------------------------------------

    async def technician_productivity(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> TechnicianProductivityReport:
        """Per-technician output over a period, busiest first.

        Three sources are combined in Python rather than in one join. Repair
        orders, tasks and labor records have three different windows and three
        different definitions of "in this period", and joining them into a single
        query multiplies rows against each other and inflates every count — the
        classic fan-out bug, which produces a confidently wrong number rather
        than an obvious error.
        """
        period = self.resolve_period(start_date, end_date)
        start, end = period.start_date, period.end_date
        window_start, window_end = _day_start(start), _day_end(end)

        ro_stmt = select(
            RepairOrder.technician_id,
            RepairOrder.started_at,
            RepairOrder.completed_at,
        ).where(
            RepairOrder.technician_id.isnot(None),
            or_(
                RepairOrder.created_at.between(window_start, window_end),
                RepairOrder.started_at.between(window_start, window_end),
                RepairOrder.completed_at.between(window_start, window_end),
            ),
        )
        rows = (await self.db.execute(ro_stmt)).all()

        assigned: dict[UUID, int] = {}
        finished: dict[UUID, int] = {}
        cycle_seconds: dict[UUID, float] = {}
        cycle_count: dict[UUID, int] = {}

        for technician_id, started_at, completed_at in rows:
            if started_at is not None and window_start <= started_at <= window_end:
                assigned[technician_id] = assigned.get(technician_id, 0) + 1
            if completed_at is not None and window_start <= completed_at <= window_end:
                finished[technician_id] = finished.get(technician_id, 0) + 1
            if (
                started_at is not None
                and completed_at is not None
                and window_start <= completed_at <= window_end
            ):
                seconds = (completed_at - started_at).total_seconds()
                cycle_seconds[technician_id] = cycle_seconds.get(technician_id, 0.0) + seconds
                cycle_count[technician_id] = cycle_count.get(technician_id, 0) + 1

        tasks_stmt = (
            select(RepairTask.assigned_to_id, func.count(RepairTask.id))
            .where(
                RepairTask.assigned_to_id.isnot(None),
                RepairTask.status == RepairTaskStatus.COMPLETED.value,
                RepairTask.completed_at >= window_start,
                RepairTask.completed_at <= window_end,
            )
            .group_by(RepairTask.assigned_to_id)
        )
        tasks_completed = {
            technician_id: int(count)
            for technician_id, count in (await self.db.execute(tasks_stmt)).all()
        }

        labor_stmt = (
            select(
                LaborRecord.technician_id,
                func.coalesce(func.sum(LaborRecord.actual_hours), 0.0),
                func.coalesce(func.sum(LaborRecord.billable_hours), 0.0),
                func.coalesce(
                    func.sum(LaborRecord.billable_hours * LaborRecord.hourly_rate), 0.0
                ),
            )
            .where(
                LaborRecord.technician_id.isnot(None),
                LaborRecord.performed_at >= window_start,
                LaborRecord.performed_at <= window_end,
            )
            .group_by(LaborRecord.technician_id)
        )
        labor: dict[UUID, tuple[float, float, float]] = {}
        for technician_id, actual, billable, revenue in (await self.db.execute(labor_stmt)).all():
            labor[technician_id] = (
                float(actual or 0.0),
                float(billable or 0.0),
                float(revenue or 0.0),
            )

        technician_ids = {
            *assigned,
            *finished,
            *cycle_count,
            *tasks_completed,
            *labor,
        }
        names = await self._user_names(technician_ids)

        report: list[TechnicianProductivity] = []
        for technician_id in technician_ids:
            actual, billable, revenue = labor.get(technician_id, (0.0, 0.0, 0.0))
            cycles = cycle_count.get(technician_id, 0)
            report.append(
                TechnicianProductivity(
                    technician_id=technician_id,
                    name=names.get(technician_id, "Unknown user"),
                    repair_orders_assigned=assigned.get(technician_id, 0),
                    repair_orders_completed=finished.get(technician_id, 0),
                    tasks_completed=tasks_completed.get(technician_id, 0),
                    labor_hours_actual=round(actual, 2),
                    labor_hours_billable=round(billable, 2),
                    labor_revenue=round_money(revenue),
                    average_cycle_hours=(
                        round(
                            cycle_seconds.get(technician_id, 0.0) / cycles / SECONDS_PER_HOUR,
                            2,
                        )
                        if cycles
                        else 0.0
                    ),
                )
            )

        report.sort(key=lambda t: (-t.labor_revenue, -t.repair_orders_completed, t.name))

        return TechnicianProductivityReport(
            period=period,
            technicians=report,
            totals=TechnicianTotals(
                technician_count=len(report),
                repair_orders_completed=sum(t.repair_orders_completed for t in report),
                tasks_completed=sum(t.tasks_completed for t in report),
                labor_hours_actual=round(sum(t.labor_hours_actual for t in report), 2),
                labor_hours_billable=round(sum(t.labor_hours_billable for t in report), 2),
                labor_revenue=round_money(sum(t.labor_revenue for t in report)),
            ),
        )

    async def _user_names(self, user_ids) -> dict[UUID, str]:
        """Resolve display names for a set of user ids in one query."""
        from app.auth.models import User

        ids = [str(uid) for uid in user_ids]
        if not ids:
            return {}
        rows = (
            await self.db.execute(select(User.id, User.first_name, User.last_name).where(
                User.id.in_(ids)
            ))
        ).all()
        return {row_id: f"{first} {last}" for row_id, first, last in rows}

    # --- inventory value ---------------------------------------------------

    async def inventory_value_report(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int = DEFAULT_TOP_LIMIT,
    ) -> InventoryValueReport:
        """What is on the shelf, what it cost, and what is running out.

        Valued at today's catalog cost. That is the right figure for "what would
        it cost to replace the shelves", and the wrong one for "what did that
        stock actually cost us" — which is why each ledger row keeps its own
        ``unit_cost`` and why the top-movers list below uses those, not this.
        """
        period = self.resolve_period(start_date, end_date)
        limit = max(1, min(int(limit), 50))

        totals = (
            await self.db.execute(
                select(
                    func.coalesce(func.sum(Part.quantity_on_hand * Part.unit_cost), 0.0),
                    func.coalesce(func.sum(Part.quantity_on_hand * Part.unit_price), 0.0),
                    func.count(Part.id),
                    func.coalesce(func.sum(Part.quantity_on_hand), 0.0),
                )
            )
        ).one()
        cost_value, retail_value, part_count, units_on_hand = totals

        by_category_rows = (
            await self.db.execute(
                select(
                    Part.category,
                    func.coalesce(func.sum(Part.quantity_on_hand * Part.unit_cost), 0.0),
                    func.coalesce(func.sum(Part.quantity_on_hand * Part.unit_price), 0.0),
                    func.count(Part.id),
                    func.coalesce(func.sum(Part.quantity_on_hand), 0.0),
                )
                .group_by(Part.category)
                .order_by(func.sum(Part.quantity_on_hand * Part.unit_cost).desc())
            )
        ).all()
        by_category = [
            InventoryCategoryValue(
                category=category,
                cost_value=round_money(float(category_cost or 0.0)),
                retail_value=round_money(float(category_retail or 0.0)),
                part_count=int(category_count or 0),
                units_on_hand=round(float(category_units or 0.0), 2),
            )
            for category, category_cost, category_retail, category_count, category_units in by_category_rows
        ]

        # A discontinued part is left out of the low-stock list on purpose: it is
        # not going to be reordered, so listing it every morning is a false alarm
        # that trains people to stop reading the list.
        low_stock_rows = (
            await self.db.execute(
                select(
                    Part.id,
                    Part.part_number,
                    Part.name,
                    Part.quantity_on_hand,
                    Part.reorder_level,
                    Part.unit_cost,
                )
                .where(
                    Part.status == PartStatus.ACTIVE.value,
                    Part.quantity_on_hand <= Part.reorder_level,
                )
                .order_by(Part.quantity_on_hand.asc(), Part.name.asc())
            )
        ).all()

        low_stock_items = [
            LowStockItem(
                part_id=row_id,
                part_number=part_number,
                name=name,
                quantity_on_hand=round(float(quantity or 0.0), 2),
                reorder_level=round(float(reorder or 0.0), 2),
                stock_status="OUT" if float(quantity or 0.0) <= 0 else "LOW",
                cost_value=round_money(float(quantity or 0.0) * float(unit_cost or 0.0)),
            )
            for row_id, part_number, name, quantity, reorder, unit_cost in low_stock_rows
        ]

        window_start, window_end = _day_start(period.start_date), _day_end(period.end_date)
        movers_rows = (
            await self.db.execute(
                select(
                    InventoryTransaction.part_id,
                    Part.part_number,
                    Part.name,
                    func.coalesce(func.sum(func.abs(InventoryTransaction.quantity)), 0.0),
                    func.coalesce(
                        func.sum(
                            func.abs(InventoryTransaction.quantity)
                            * func.coalesce(InventoryTransaction.unit_cost, 0.0)
                        ),
                        0.0,
                    ),
                )
                .join(Part, Part.id == InventoryTransaction.part_id)
                .where(
                    InventoryTransaction.transaction_type
                    == InventoryTransactionType.ISSUE.value,
                    InventoryTransaction.created_at >= window_start,
                    InventoryTransaction.created_at <= window_end,
                )
                .group_by(InventoryTransaction.part_id, Part.part_number, Part.name)
                .order_by(
                    func.sum(
                        func.abs(InventoryTransaction.quantity)
                        * func.coalesce(InventoryTransaction.unit_cost, 0.0)
                    ).desc()
                )
                .limit(limit)
            )
        ).all()

        total_cost = round_money(float(cost_value or 0.0))
        total_retail = round_money(float(retail_value or 0.0))

        return InventoryValueReport(
            as_of=period.end_date,
            cost_value=total_cost,
            retail_value=total_retail,
            potential_margin=round_money(total_retail - total_cost),
            part_count=int(part_count or 0),
            units_on_hand=round(float(units_on_hand or 0.0), 2),
            low_stock_count=len(low_stock_items),
            out_of_stock_count=sum(1 for item in low_stock_items if item.stock_status == "OUT"),
            by_category=by_category,
            low_stock_items=low_stock_items[:limit],
            top_movers=[
                StockMover(
                    part_id=part_id,
                    part_number=part_number,
                    name=name,
                    quantity_issued=round(float(quantity or 0.0), 2),
                    value_issued=round_money(float(value or 0.0)),
                )
                for part_id, part_number, name, quantity, value in movers_rows
            ],
        )

    # --- customer retention ------------------------------------------------

    async def customer_retention_report(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int = DEFAULT_TOP_LIMIT,
    ) -> CustomerRetentionReport:
        """Whether customers come back, and who they are."""
        period = self.resolve_period(start_date, end_date)
        start, end = period.start_date, period.end_date
        limit = max(1, min(int(limit), 50))

        # Only settled bills count. Revenue that was invoiced and never paid
        # describes a customer who has not yet been retained by anyone.
        period_stmt = (
            select(
                Invoice.customer_id,
                func.coalesce(func.sum(Invoice.total), 0.0),
                func.count(Invoice.id),
            )
            .where(
                Invoice.status == InvoiceStatus.PAID.value,
                Invoice.invoice_date >= start,
                Invoice.invoice_date <= end,
            )
            .group_by(Invoice.customer_id)
        )
        period_rows = (await self.db.execute(period_stmt)).all()

        # Lifetime history, for the one question the window cannot answer: has
        # this person ever been here before?
        lifetime_stmt = (
            select(
                Invoice.customer_id,
                func.count(Invoice.id),
                func.min(Invoice.invoice_date),
            )
            .where(Invoice.status == InvoiceStatus.PAID.value)
            .group_by(Invoice.customer_id)
        )
        lifetime = {
            customer_id: (int(count), first_paid)
            for customer_id, count, first_paid in (await self.db.execute(lifetime_stmt)).all()
        }

        customers_billed = len(period_rows)
        returning = 0
        new_customers = 0
        settled_revenue = 0.0

        for customer_id, revenue, _count in period_rows:
            settled_revenue = round_money(settled_revenue + float(revenue or 0.0))
            total_invoices, first_paid = lifetime.get(customer_id, (0, None))
            if total_invoices > 1:
                returning += 1
            if first_paid is not None and start <= first_paid <= end:
                new_customers += 1

        top_ids = [
            customer_id
            for customer_id, _revenue, _count in sorted(
                period_rows,
                key=lambda row: (-float(row[1] or 0.0), str(row[0])),
            )[:limit]
        ]
        names = await self._customer_names(top_ids)
        top_customers = sorted(
            (
                CustomerRevenueRow(
                    customer_id=customer_id,
                    name=names.get(customer_id, "Unknown customer"),
                    revenue=round_money(float(revenue or 0.0)),
                    invoice_count=int(count or 0),
                )
                for customer_id, revenue, count in period_rows
            ),
            key=lambda row: (-row.revenue, row.invoice_count, row.customer_id.hex),
        )[:limit]

        return CustomerRetentionReport(
            period=period,
            customers_billed=customers_billed,
            new_customers=new_customers,
            returning_customers=returning,
            repeat_rate=round(returning / customers_billed, 4) if customers_billed else 0.0,
            settled_revenue=settled_revenue,
            revenue_per_customer=(
                round_money(settled_revenue / customers_billed) if customers_billed else 0.0
            ),
            top_customers=top_customers,
        )

    async def _customer_names(self, customer_ids) -> dict[UUID, str]:
        """Resolve display names for customers in one query."""
        ids = [str(cid) for cid in customer_ids]
        if not ids:
            return {}
        rows = (
            await self.db.execute(
                select(Customer.id, Customer.first_name, Customer.last_name, Customer.company_name).where(
                    Customer.id.in_(ids)
                )
            )
        ).all()
        return {
            row_id: (f"{company} ({first} {last})" if company else f"{first} {last}")
            for row_id, first, last, company in rows
        }

    # --- dashboard ---------------------------------------------------------

    async def dashboard(
        self,
        user_id: UUID | str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> DashboardReport:
        """One call for the screen the shop looks at all day.

        Built from the same queries as the individual reports, so the dashboard
        cannot drift from the report it summarises. The period governs the money;
        the queue figures are always "now", because a queue is not a historical
        quantity and filtering it by date would show a shop that is currently
        on fire reporting a calm March.
        """
        period = self.resolve_period(start_date, end_date)
        start, end = period.start_date, period.end_date
        window_start, window_end = _day_start(start), _day_end(end)

        invoiced_stmt = select(func.coalesce(func.sum(Invoice.total), 0.0)).where(
            Invoice.status.in_(INVOICED_STATUSES),
            Invoice.invoice_date >= start,
            Invoice.invoice_date <= end,
        )
        collected_stmt = select(func.coalesce(func.sum(Payment.amount), 0.0)).where(
            Payment.status == PaymentStatus.RECORDED.value,
            Payment.payment_date >= start,
            Payment.payment_date <= end,
        )
        invoiced_total = float((await self.db.execute(invoiced_stmt)).scalar_one() or 0.0)
        collected_total = float((await self.db.execute(collected_stmt)).scalar_one() or 0.0)

        outstanding = await self._outstanding()

        ro_rows = (
            await self.db.execute(
                select(RepairOrder.status, func.count(RepairOrder.id)).group_by(
                    RepairOrder.status
                )
            )
        ).all()
        ro_counts = {status: int(count) for status, count in ro_rows}

        completed_in_period = await self._count_in_window(
            RepairOrder.completed_at, window_start, window_end
        )

        invoice_rows = (
            await self.db.execute(
                select(Invoice.status, func.count(Invoice.id)).group_by(Invoice.status)
            )
        ).all()
        invoice_counts = {status: int(count) for status, count in invoice_rows}

        inventory_cost = float(
            (
                await self.db.execute(
                    select(
                        func.coalesce(func.sum(Part.quantity_on_hand * Part.unit_cost), 0.0)
                    )
                )
            ).scalar_one()
            or 0.0
        )

        low_stock_stmt = select(func.count(Part.id)).where(
            Part.status == PartStatus.ACTIVE.value,
            Part.quantity_on_hand <= Part.reorder_level,
        )
        low_stock_count = int(
            (await self.db.execute(low_stock_stmt)).scalar_one() or 0
        )
        out_of_stock_count = int(
            (
                await self.db.execute(
                    select(func.count(Part.id)).where(
                        Part.status == PartStatus.ACTIVE.value,
                        Part.quantity_on_hand <= 0,
                    )
                )
            ).scalar_one()
            or 0
        )

        today_start, today_end = _day_start(today()), _day_end(today())
        appointments_today = int(
            (
                await self.db.execute(
                    select(func.count(Appointment.id)).where(
                        Appointment.scheduled_start >= today_start,
                        Appointment.scheduled_start <= today_end,
                    )
                )
            ).scalar_one()
            or 0
        )
        vehicles_in_shop = int(
            (
                await self.db.execute(
                    select(func.count(Appointment.id)).where(
                        Appointment.status.in_(ACTIVE_IN_SHOP_STATUSES)
                    )
                )
            ).scalar_one()
            or 0
        )
        pending_part_requests = int(
            (
                await self.db.execute(
                    select(func.count(PartRequest.id)).where(
                        PartRequest.status == PartRequestStatus.PENDING.value
                    )
                )
            ).scalar_one()
            or 0
        )
        unread_notifications = int(
            (
                await self.db.execute(
                    select(func.count(Notification.id)).where(
                        Notification.recipient_id == str(user_id),
                        Notification.read_at.is_(None),
                    )
                )
            ).scalar_one()
            or 0
        )

        logger.debug("Dashboard built for user %s over %s..%s", user_id, start, end)

        return DashboardReport(
            period=period,
            generated_for=UUID(str(user_id)),
            revenue=DashboardRevenue(
                invoiced_total=round_money(invoiced_total),
                collected_total=round_money(collected_total),
                outstanding_balance=outstanding["balance"],
                overdue_balance=outstanding["overdue_balance"],
                overdue_count=outstanding["overdue_count"],
            ),
            work=DashboardWork(
                repair_orders_open=sum(
                    ro_counts.get(status, 0)
                    for status in (
                        RepairOrderStatus.APPROVED.value,
                        RepairOrderStatus.IN_PROGRESS.value,
                        RepairOrderStatus.ON_HOLD.value,
                    )
                ),
                repair_orders_in_progress=ro_counts.get(
                    RepairOrderStatus.IN_PROGRESS.value, 0
                ),
                repair_orders_on_hold=ro_counts.get(
                    RepairOrderStatus.ON_HOLD.value, 0
                ),
                # A COMPLETED order has had the work finished but has not been
                # through quality control yet — that is the queue a shop forgets.
                repair_orders_awaiting_qc=ro_counts.get(
                    RepairOrderStatus.COMPLETED.value, 0
                ),
                completed_in_period=completed_in_period,
                part_requests_pending=pending_part_requests,
            ),
            money=DashboardMoney(
                invoices_draft=invoice_counts.get(InvoiceStatus.DRAFT.value, 0),
                invoices_issued=invoice_counts.get(InvoiceStatus.ISSUED.value, 0),
                invoices_partially_paid=invoice_counts.get(
                    InvoiceStatus.PARTIALLY_PAID.value, 0
                ),
                invoices_paid=invoice_counts.get(InvoiceStatus.PAID.value, 0),
            ),
            operations=DashboardOperations(
                low_stock_count=low_stock_count,
                out_of_stock_count=out_of_stock_count,
                inventory_cost_value=round_money(inventory_cost),
                appointments_today=appointments_today,
                vehicles_in_shop=vehicles_in_shop,
                pending_part_requests=pending_part_requests,
                unread_notifications=unread_notifications,
            ),
        )


__all__ = [
    "INVOICED_STATUSES",
    "ReportService",
]
