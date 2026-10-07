"""API routes for invoicing.

Endpoints:
- GET    /                          List invoices (paginated, filterable)
- POST   /                          Raise an invoice against a repair order
- GET    /{id}                      Get an invoice
- GET    /{id}/summary              Customer-facing money summary
- PATCH  /{id}                      Update a draft invoice
- DELETE /{id}                      Delete a draft invoice
- POST   /{id}/items                Add a shop-raised charge line
- PATCH  /{id}/items/{item_id}      Correct a shop-raised charge line
- DELETE /{id}/items/{item_id}      Remove a shop-raised charge line
- POST   /{id}/issue                Send the invoice to the customer
- POST   /{id}/void                 Write an issued invoice off

There is deliberately no generic status endpoint. ``PARTIALLY_PAID`` and
``PAID`` are reached only by recording a payment through :mod:`app.payments`,
so an invoice can never claim money that was never taken.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.audit.decorator import audit
from app.audit.models import AuditAction
from app.auth.dependencies import require_permission
from app.auth.models import User
from app.auth.permissions import PermissionEnum
from app.common.schemas import PaginatedResponse
from app.core.database import AsyncSession, get_session
from app.invoices.schemas import (
    InvoiceCreate,
    InvoiceItemCreate,
    InvoiceItemUpdate,
    InvoiceRead,
    InvoiceSummary,
    InvoiceUpdate,
)
from app.invoices.services import InvoiceService

router = APIRouter()


@router.get("/", response_model=PaginatedResponse[InvoiceRead])
async def list_invoices(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    customer_id: UUID | None = Query(None),
    vehicle_id: UUID | None = Query(None),
    repair_order_id: UUID | None = Query(None),
    search: str | None = Query(None, max_length=30),
    overdue_only: bool = Query(False),
    unpaid_only: bool = Query(False),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    current_user: User = Depends(require_permission(PermissionEnum.INVOICES_READ)),
    session: AsyncSession = Depends(get_session),
):
    """List invoices with pagination and filtering, newest first."""
    service = InvoiceService(session)
    invoices, total = await service.list_invoices(
        page=page,
        size=size,
        status=status,
        customer_id=customer_id,
        vehicle_id=vehicle_id,
        repair_order_id=repair_order_id,
        search=search,
        overdue_only=overdue_only,
        unpaid_only=unpaid_only,
        start_date=start_date,
        end_date=end_date,
    )
    return PaginatedResponse[InvoiceRead].create(
        items=[InvoiceRead.model_validate(i) for i in invoices],
        page=page,
        size=size,
        total=total,
    )


@router.post("/", response_model=InvoiceRead, status_code=status.HTTP_201_CREATED)
@audit(AuditAction.CREATE, "invoice")
async def create_invoice(
    invoice_data: InvoiceCreate,
    current_user: User = Depends(require_permission(PermissionEnum.INVOICES_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Raise a draft invoice against a finished repair order.

    The approved lines of the order's estimate are copied on automatically; only
    the shop's own additions have to be supplied.
    """
    service = InvoiceService(session)
    invoice = await service.create_invoice(invoice_data, created_by_id=current_user.id)
    return InvoiceRead.model_validate(invoice)


@router.get("/{invoice_id}", response_model=InvoiceRead)
async def get_invoice(
    invoice_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.INVOICES_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get an invoice with its charge lines."""
    service = InvoiceService(session)
    return InvoiceRead.model_validate(await service.get_by_id(invoice_id))


@router.get("/{invoice_id}/summary", response_model=InvoiceSummary)
async def get_invoice_summary(
    invoice_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.INVOICES_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get the customer-facing money summary for an invoice."""
    service = InvoiceService(session)
    return await service.get_summary(invoice_id)


@router.patch("/{invoice_id}", response_model=InvoiceRead)
@audit(AuditAction.UPDATE, "invoice")
async def update_invoice(
    invoice_id: UUID,
    invoice_data: InvoiceUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.INVOICES_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Update a draft invoice's dates, tax rate or notes."""
    service = InvoiceService(session)
    invoice = await service.update_invoice(invoice_id, invoice_data)
    return InvoiceRead.model_validate(invoice)


@router.delete("/{invoice_id}", status_code=status.HTTP_204_NO_CONTENT)
@audit(AuditAction.DELETE, "invoice")
async def delete_invoice(
    invoice_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.INVOICES_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Delete a draft invoice the customer has not been sent."""
    service = InvoiceService(session)
    await service.delete_invoice(invoice_id)


@router.post(
    "/{invoice_id}/items", response_model=InvoiceRead, status_code=status.HTTP_201_CREATED
)
@audit(AuditAction.UPDATE, "invoice", summary="Added a charge line to an invoice")
async def add_invoice_item(
    invoice_id: UUID,
    item_data: InvoiceItemCreate,
    current_user: User = Depends(require_permission(PermissionEnum.INVOICES_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Add a shop-raised charge line to a draft invoice."""
    service = InvoiceService(session)
    invoice = await service.add_item(invoice_id, item_data)
    return InvoiceRead.model_validate(invoice)


@router.patch("/{invoice_id}/items/{item_id}", response_model=InvoiceRead)
@audit(
    AuditAction.UPDATE, "invoice", id_param="invoice_id",
    summary="Corrected a charge line on an invoice",
)
async def update_invoice_item(
    invoice_id: UUID,
    item_id: UUID,
    item_data: InvoiceItemUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.INVOICES_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Correct a shop-raised charge line on a draft invoice."""
    service = InvoiceService(session)
    invoice = await service.update_item(invoice_id, item_id, item_data)
    return InvoiceRead.model_validate(invoice)


@router.delete("/{invoice_id}/items/{item_id}", response_model=InvoiceRead)
@audit(
    AuditAction.UPDATE, "invoice", id_param="invoice_id",
    summary="Removed a charge line from an invoice",
)
async def delete_invoice_item(
    invoice_id: UUID,
    item_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.INVOICES_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Remove a shop-raised charge line from a draft invoice."""
    service = InvoiceService(session)
    invoice = await service.delete_item(invoice_id, item_id)
    return InvoiceRead.model_validate(invoice)


@router.post("/{invoice_id}/issue", response_model=InvoiceRead)
@audit(AuditAction.ISSUE, "invoice")
async def issue_invoice(
    invoice_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.INVOICES_MANAGE)),
    session: AsyncSession = Depends(get_session),
):
    """Send a draft invoice to the customer.

    Issuing is the point of no return for the lines: the customer now holds the
    document, so the invoice stops being editable and the balance becomes owed.
    """
    service = InvoiceService(session)
    invoice = await service.issue(invoice_id)
    return InvoiceRead.model_validate(invoice)


@router.post("/{invoice_id}/void", response_model=InvoiceRead)
@audit(AuditAction.STATUS_CHANGE, "invoice", summary="Voided an invoice")
async def void_invoice(
    invoice_id: UUID,
    reason: Annotated[str, Query(min_length=1, max_length=1000)],
    current_user: User = Depends(require_permission(PermissionEnum.INVOICES_MANAGE)),
    session: AsyncSession = Depends(get_session),
):
    """Write an issued invoice off, recording why.

    Held on ``invoices:manage`` rather than ``invoices:write`` because writing off
    a bill is a decision about money owed, not an edit to a document.
    """
    service = InvoiceService(session)
    invoice = await service.void_invoice(invoice_id, reason)
    return InvoiceRead.model_validate(invoice)
