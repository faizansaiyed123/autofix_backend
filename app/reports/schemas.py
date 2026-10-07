"""Pydantic schemas for reports and analytics.

Every schema here describes a **reading** — a figure that was true when it was
asked for. None of them is a persisted document, which is why there is no
``Create``, no ``Update`` and no identifier to look one up by.

Two shapes recur. A report is a ``period`` plus whatever sections it is made of,
and each section carries its own denominator where a rate is reported: an
average invoice value without the invoice count is a number nobody can argue
with, and that is exactly why it should not be shown on its own.
"""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import Field

from app.common.schemas import BaseSchema

# Bucket sizes for a report's time series. DAY is only used for a short window:
# a daily point per day across five years is a thousand buckets of mostly zeroes,
# which is noise with a query plan attached.
GRANULARITY_DAY = "DAY"
GRANULARITY_MONTH = "MONTH"
GRANULARITY_VALUES: tuple[str, ...] = (GRANULARITY_DAY, GRANULARITY_MONTH)

# The longest window still charted by day. 62 days covers two months of trading
# at daily resolution; past that the series switches to months on its own.
MAX_DAY_GRANULARITY_DAYS = 62

# The window a report covers when the caller names no dates. Thirty days is the
# period a garage actually reviews; "all time" is never the question anybody asks
# and is unbounded on an append-only ledger.
DEFAULT_REPORT_DAYS = 30

# Ceiling on the "top N" lists. These are for a screen, not for an export, and a
# list of four thousand parts is not a report of what moved, it is a table dump.
DEFAULT_TOP_LIMIT = 10
MAX_TOP_LIMIT = 50


class ReportPeriod(BaseSchema):
    """The window a report covers, and how finely it is sliced."""

    start_date: date
    end_date: date
    days: int = Field(..., description="Inclusive day count of the window")
    granularity: str = Field(..., description="DAY or MONTH")


class RevenuePoint(BaseSchema):
    """One bucket of the revenue series."""

    period_start: date
    invoiced: float = Field(..., description="Charged on invoices that left the counter")
    collected: float = Field(..., description="Payments actually recorded, net of voids")


class RevenueReport(BaseSchema):
    """What the shop charged, and what it actually took, over a period.

    ``invoiced_total`` and ``collected_total`` are separate figures on purpose.
    Money owed is not money received, and a report that adds them together is
    reporting a number that describes no real thing.
    """

    period: ReportPeriod
    invoiced_total: float
    invoice_count: int
    average_invoice: float
    collected_total: float
    payment_count: int
    voided_total: float
    void_count: int
    outstanding_balance: float
    outstanding_count: int
    overdue_balance: float
    overdue_count: int
    invoiced_by_status: dict[str, float] = Field(default_factory=dict)
    series: list[RevenuePoint] = Field(default_factory=list)


class RepairOrderSeriesPoint(BaseSchema):
    """One bucket of the repair-order series."""

    period_start: date
    opened: int
    completed: int


class RepairOrderReport(BaseSchema):
    """How work is flowing through the shop.

    ``status_counts`` is a **snapshot of every repair order that exists** and is
    deliberately not filtered by the period: "how many are sitting in QC right
    now" is a question about today, and answering it with a March-only figure
    would report a healthy queue that is in fact on fire. The period counts below
    it are the ones the window applies to.
    """

    period: ReportPeriod
    status_counts: dict[str, int] = Field(default_factory=dict)
    total_orders: int = Field(..., description="Repair orders created inside the period")
    opened: int = Field(..., description="Work started inside the period")
    completed: int = Field(..., description="Work finished inside the period")
    delivered: int
    cancelled: int
    completion_rate: float = Field(..., ge=0.0, le=1.0)
    average_cycle_hours: float = Field(
        ..., description="Mean hours from first work started to finished, for orders that finished in the period"
    )
    series: list[RepairOrderSeriesPoint] = Field(default_factory=list)


class TechnicianProductivity(BaseSchema):
    """One technician's output over a period.

    A technician appears here because they *did work in the period*, not because
    they hold the TECHNICIAN role. Whoever turned the spanner is the person the
    numbers are about, and a shop where the parts clerk covered a Saturday would
    be misreported by a report that only looked at job titles.
    """

    technician_id: uuid.UUID
    name: str
    repair_orders_assigned: int
    repair_orders_completed: int
    tasks_completed: int
    labor_hours_actual: float
    labor_hours_billable: float
    labor_revenue: float
    average_cycle_hours: float


class TechnicianTotals(BaseSchema):
    """The whole workshop's output, so one technician cannot be read alone."""

    technician_count: int
    repair_orders_completed: int
    tasks_completed: int
    labor_hours_actual: float
    labor_hours_billable: float
    labor_revenue: float


class TechnicianProductivityReport(BaseSchema):
    period: ReportPeriod
    technicians: list[TechnicianProductivity] = Field(default_factory=list)
    totals: TechnicianTotals


class InventoryCategoryValue(BaseSchema):
    """Stock value for one catalog category."""

    category: str
    cost_value: float
    retail_value: float
    part_count: int
    units_on_hand: float


class LowStockItem(BaseSchema):
    """A part at or below its reorder point."""

    part_id: uuid.UUID
    part_number: str
    name: str
    quantity_on_hand: float
    reorder_level: float
    stock_status: str = Field(..., description="LOW or OUT")
    cost_value: float


class StockMover(BaseSchema):
    """A part that consumed the most stock value in the period."""

    part_id: uuid.UUID
    part_number: str
    name: str
    quantity_issued: float
    value_issued: float


class InventoryValueReport(BaseSchema):
    """What is on the shelf, what it cost, and what is running out.

    Valued at the catalog's current ``unit_cost``, which is today's price and not
    a historical one — this is a balance-sheet figure ("what would replacing this
    stock cost us now"), not a profit statement.
    """

    as_of: date
    cost_value: float
    retail_value: float
    potential_margin: float
    part_count: int
    units_on_hand: float
    low_stock_count: int
    out_of_stock_count: int
    by_category: list[InventoryCategoryValue] = Field(default_factory=list)
    low_stock_items: list[LowStockItem] = Field(default_factory=list)
    top_movers: list[StockMover] = Field(default_factory=list)


class CustomerRevenueRow(BaseSchema):
    """One customer's settled spend in the period."""

    customer_id: uuid.UUID
    name: str
    revenue: float
    invoice_count: int


class CustomerRetentionReport(BaseSchema):
    """Whether customers come back.

    "Retained" means settled more than one bill **ever**, not more than one in
    this window — a customer who returned in March after a year away is exactly
    the person this report exists to find, and counting only within the window
    would report the shop as full of one-timers.
    """

    period: ReportPeriod
    customers_billed: int
    new_customers: int
    returning_customers: int
    repeat_rate: float = Field(..., ge=0.0, le=1.0)
    settled_revenue: float
    revenue_per_customer: float
    top_customers: list[CustomerRevenueRow] = Field(default_factory=list)


class DashboardRevenue(BaseSchema):
    """The money half of the dashboard."""

    invoiced_total: float
    collected_total: float
    outstanding_balance: float
    overdue_balance: float
    overdue_count: int


class DashboardWork(BaseSchema):
    """The work-in-progress half of the dashboard."""

    repair_orders_open: int
    repair_orders_in_progress: int
    repair_orders_on_hold: int
    repair_orders_awaiting_qc: int
    completed_in_period: int
    part_requests_pending: int


class DashboardMoney(BaseSchema):
    """The billing half of the dashboard."""

    invoices_draft: int
    invoices_issued: int
    invoices_partially_paid: int
    invoices_paid: int


class DashboardOperations(BaseSchema):
    """The floor half of the dashboard: stock, appointments, attention."""

    low_stock_count: int
    out_of_stock_count: int
    inventory_cost_value: float
    appointments_today: int
    vehicles_in_shop: int
    pending_part_requests: int
    unread_notifications: int


class DashboardReport(BaseSchema):
    """One date-filtered call for the screen the shop looks at all day.

    Assembled from the same queries the individual reports use, so the dashboard
    can never disagree with the report it is a summary of. Counts that describe
    the present (what is open, what is unread) are **not** filtered by the
    period: the window governs the money, and the queue is always "now".
    """

    period: ReportPeriod
    generated_for: uuid.UUID
    revenue: DashboardRevenue
    work: DashboardWork
    money: DashboardMoney
    operations: DashboardOperations


__all__ = [
    "DEFAULT_REPORT_DAYS",
    "DEFAULT_TOP_LIMIT",
    "GRANULARITY_DAY",
    "GRANULARITY_MONTH",
    "GRANULARITY_VALUES",
    "MAX_DAY_GRANULARITY_DAYS",
    "MAX_TOP_LIMIT",
    "CustomerRetentionReport",
    "CustomerRevenueRow",
    "DashboardMoney",
    "DashboardOperations",
    "DashboardReport",
    "DashboardRevenue",
    "DashboardWork",
    "InventoryCategoryValue",
    "InventoryValueReport",
    "LowStockItem",
    "RepairOrderReport",
    "RepairOrderSeriesPoint",
    "ReportPeriod",
    "RevenuePoint",
    "RevenueReport",
    "StockMover",
    "TechnicianProductivity",
    "TechnicianProductivityReport",
    "TechnicianTotals",
]
