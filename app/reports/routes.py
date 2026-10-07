"""API routes for reports and analytics.

Endpoints:
- GET /dashboard          One call for the shop's morning screen
- GET /revenue            What was charged, and what was actually collected
- GET /repair-orders      How work is flowing through the shop
- GET /technicians        Per-technician output over a period
- GET /inventory-value    Stock on hand, at cost, and what is running out
- GET /customer-retention Whether customers come back, and who they are

Nothing here writes. A report is a reading taken from the tables that own the
facts, so there is no POST, no PUT and no DELETE anywhere in this router — and
that absence is the design, not an omission. The moment a report can be edited,
it is no longer a report of what happened.

Two permission levels separate the shop's numbers from its judgement of people.
``reports:read`` covers the operational reports every member of staff needs.
``reports:analytics`` covers the two that rank *named* individuals —
technicians against each other, customers by what they are worth — and is held
by the owner alone. Stock valuation sits behind ``inventory:read`` rather than
``reports:read``, because it is the shop's own purchase cost per part, and
publishing the margin on the whole catalog to the front desk is not a reporting
decision at all.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query

from app.auth.dependencies import require_permission
from app.auth.models import User
from app.auth.permissions import PermissionEnum
from app.core.database import AsyncSession, get_session
from app.reports.schemas import (
    DEFAULT_TOP_LIMIT,
    MAX_TOP_LIMIT,
    CustomerRetentionReport,
    DashboardReport,
    InventoryValueReport,
    RepairOrderReport,
    RevenueReport,
    TechnicianProductivityReport,
)
from app.reports.services import ReportService

router = APIRouter()


@router.get("/dashboard", response_model=DashboardReport)
async def get_dashboard(
    start_date: date | None = Query(None, description="Defaults to 30 days ago"),
    end_date: date | None = Query(None, description="Defaults to today"),
    current_user: User = Depends(require_permission(PermissionEnum.REPORTS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """The shop's operational picture in one call.

    Assembled from the same queries the individual reports use, so it can never
    disagree with the report it summarises. The period governs the money; the
    queue counts are always "now".
    """
    service = ReportService(session)
    return await service.dashboard(current_user.id, start_date=start_date, end_date=end_date)


@router.get("/revenue", response_model=RevenueReport)
async def get_revenue_report(
    start_date: date | None = Query(None, description="Defaults to 30 days ago"),
    end_date: date | None = Query(None, description="Defaults to today"),
    current_user: User = Depends(require_permission(PermissionEnum.REPORTS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """What the shop charged, and what the till actually took.

    ``invoiced_total`` and ``collected_total`` are reported separately on
    purpose: money owed is not money received, and the difference between them is
    the number a garage most needs to see and least often does.
    """
    service = ReportService(session)
    return await service.revenue_report(start_date=start_date, end_date=end_date)


@router.get("/repair-orders", response_model=RepairOrderReport)
async def get_repair_order_report(
    start_date: date | None = Query(None, description="Defaults to 30 days ago"),
    end_date: date | None = Query(None, description="Defaults to today"),
    current_user: User = Depends(require_permission(PermissionEnum.REPORTS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """How work moved through the shop over a period.

    ``status_counts`` is a live snapshot of every repair order that exists, not a
    period count — a queue that is backed up today is a queue that is backed up,
    whatever March looked like.
    """
    service = ReportService(session)
    return await service.repair_order_report(start_date=start_date, end_date=end_date)


@router.get("/technicians", response_model=TechnicianProductivityReport)
async def get_technician_productivity(
    start_date: date | None = Query(None, description="Defaults to 30 days ago"),
    end_date: date | None = Query(None, description="Defaults to today"),
    current_user: User = Depends(require_permission(PermissionEnum.REPORTS_ANALYTICS)),
    session: AsyncSession = Depends(get_session),
):
    """Per-technician output: jobs finished, hours logged, revenue earned.

    Held on ``reports:analytics`` rather than ``reports:read``. This is a ranking
    of named colleagues, and a report that judges people is management's to read.
    """
    service = ReportService(session)
    return await service.technician_productivity(start_date=start_date, end_date=end_date)


@router.get("/inventory-value", response_model=InventoryValueReport)
async def get_inventory_value_report(
    start_date: date | None = Query(None, description="Used only for the top-movers list"),
    end_date: date | None = Query(None, description="Used only for the top-movers list"),
    limit: int = Query(DEFAULT_TOP_LIMIT, ge=1, le=MAX_TOP_LIMIT),
    current_user: User = Depends(require_permission(PermissionEnum.INVENTORY_READ)),
    session: AsyncSession = Depends(get_session),
):
    """What is on the shelf, what it cost, and what is running out.

    Held on ``inventory:read`` rather than ``reports:read``. The valuation is the
    shop's own purchase cost per part, so putting it behind "can read reports"
    would publish the margin on the entire catalog to everybody who can count
    invoices — which is a margin decision for the parts desk, not a reporting one.
    """
    service = ReportService(session)
    return await service.inventory_value_report(
        start_date=start_date, end_date=end_date, limit=limit
    )


@router.get("/customer-retention", response_model=CustomerRetentionReport)
async def get_customer_retention(
    start_date: date | None = Query(None, description="Defaults to 30 days ago"),
    end_date: date | None = Query(None, description="Defaults to today"),
    limit: int = Query(DEFAULT_TOP_LIMIT, ge=1, le=MAX_TOP_LIMIT),
    current_user: User = Depends(require_permission(PermissionEnum.REPORTS_ANALYTICS)),
    session: AsyncSession = Depends(get_session),
):
    """Whether customers come back, and who they are.

    "Retained" means settled more than one bill ever, not more than one inside
    this window — a customer who came back after a year away is precisely the one
    this report exists to find.
    """
    service = ReportService(session)
    return await service.customer_retention_report(
        start_date=start_date, end_date=end_date, limit=limit
    )
