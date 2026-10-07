"""API routes for payments.

Endpoints:
- GET    /                  List payments (paginated, filterable)
- POST   /                  Record a payment against an invoice
- GET    /{id}              Get a payment
- POST   /{id}/void         Reverse a payment
- GET    /summary           Takings over a period, net of reversals

Payments are never edited or deleted through the API. A payment that turns out
to be wrong is voided: the row stays as the record of what happened, and the money
goes back on the invoice's balance.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.audit.decorator import audit
from app.audit.models import AuditAction
from app.auth.dependencies import require_permission
from app.auth.models import User
from app.auth.permissions import PermissionEnum
from app.common.schemas import PaginatedResponse
from app.core.database import AsyncSession, get_session
from app.payments.schemas import (
    PaymentCreate,
    PaymentRead,
    PaymentSummary,
    PaymentVoid,
)
from app.payments.services import PaymentService

router = APIRouter()


@router.get("/", response_model=PaginatedResponse[PaymentRead])
async def list_payments(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    invoice_id: UUID | None = Query(None),
    customer_id: UUID | None = Query(None),
    method: str | None = Query(None),
    status: str | None = Query(None, description="RECORDED, VOID"),
    search: str | None = Query(None, max_length=100, description="Bank or till reference"),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    current_user: User = Depends(require_permission(PermissionEnum.PAYMENTS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """List payments with pagination and filtering, most recent first."""
    service = PaymentService(session)
    payments, total = await service.list_payments(
        page=page,
        size=size,
        invoice_id=invoice_id,
        customer_id=customer_id,
        method=method,
        status=status,
        search=search,
        start_date=start_date,
        end_date=end_date,
    )
    return PaginatedResponse[PaymentRead].create(
        items=[PaymentRead.model_validate(p) for p in payments],
        page=page,
        size=size,
        total=total,
    )


@router.post("/", response_model=PaymentRead, status_code=status.HTTP_201_CREATED)
@audit(AuditAction.RECORD_PAYMENT, "payment")
async def record_payment(
    payment_data: PaymentCreate,
    current_user: User = Depends(require_permission(PermissionEnum.PAYMENTS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Record money taken against an invoice.

    The amount may be less than the invoice's balance: a bill can be settled over
    several visits, and the remainder stays owed. It may not be more — an
    overpayment is a credit, not a negative bill.
    """
    service = PaymentService(session)
    payment = await service.record_payment(payment_data, recorded_by_id=current_user.id)
    return PaymentRead.model_validate(payment)


@router.get("/summary", response_model=PaymentSummary)
async def get_payment_summary(
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    current_user: User = Depends(require_permission(PermissionEnum.PAYMENTS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Takings over a period, net of anything reversed."""
    service = PaymentService(session)
    return await service.get_summary(start_date=start_date, end_date=end_date)


@router.get("/{payment_id}", response_model=PaymentRead)
async def get_payment(
    payment_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.PAYMENTS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get a single payment."""
    service = PaymentService(session)
    return PaymentRead.model_validate(await service.get_by_id(payment_id))


@router.post("/{payment_id}/void", response_model=PaymentRead)
@audit(AuditAction.VOID_PAYMENT, "payment")
async def void_payment(
    payment_id: UUID,
    void_data: PaymentVoid,
    current_user: User = Depends(require_permission(PermissionEnum.PAYMENTS_REFUND)),
    session: AsyncSession = Depends(get_session),
):
    """Reverse a payment and put the money back on the invoice's balance.

    Held on ``payments:refund`` rather than ``payments:write``: a customer may
    settle their own bill, but only the shop decides what leaves the till.
    """
    service = PaymentService(session)
    payment = await service.void_payment(payment_id, void_data.reason)
    return PaymentRead.model_validate(payment)
