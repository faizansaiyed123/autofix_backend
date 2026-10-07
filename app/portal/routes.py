"""API routes for the customer portal.

Endpoints:
- GET    /                      Account summary: what needs doing, what is owed
- GET    /vehicles              The customer's vehicles
- GET    /vehicles/{id}         One vehicle and its whole service history
- GET    /appointments          The customer's appointments
- POST   /appointments          Ask the shop for a slot
- GET    /service-requests      Requests the customer has sent
- POST   /service-requests      Ask the shop for work
- GET    /estimates             Estimates, optionally only those awaiting a decision
- GET    /estimates/{id}        One estimate with its lines
- POST   /estimates/{id}/items/{item_id}/decision   Approve or decline one line
- GET    /invoices              Invoices, optionally only those unpaid
- GET    /invoices/{id}         One invoice
- POST   /invoices/{id}/payments  Settle part or all of an invoice
- GET    /invoices/{id}/document Printable invoice document
- GET    /estimates/{id}/document Printable estimate document
- GET    /payments              Payments the customer has made

Every route is scoped to the customer behind the signed-in user. Staff use the
shop's own endpoints, which are behind a staff gate; the portal exists so a
customer can see and act on their own account without the shop having to hand
out staff credentials, and without the shop's endpoints becoming a way to read
everybody's.

The gates are permissions the ``CUSTOMER`` role actually holds. Notably the
account summary is gated on ``vehicles:read`` rather than ``customers:read``:
``customers:read`` opens the shop's *customer list*, so a customer must not hold
it, and granting it just to satisfy this route would hand every customer a view
of everybody else's account. The portal needs no such permission, because the
customer is derived from the token rather than chosen by the caller.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import HTMLResponse

from app.audit.decorator import audit
from app.audit.models import AuditAction
from app.auth.dependencies import require_permission
from app.auth.models import User
from app.auth.permissions import PermissionEnum
from app.common.dependencies import get_portal_service
from app.estimates.schemas import ItemDecision
from app.portal.documents import render_estimate_document, render_invoice_document
from app.portal.schemas import (
    PortalAccountSummary,
    PortalAppointment,
    PortalAppointmentCreate,
    PortalDecisionResult,
    PortalEstimate,
    PortalInvoice,
    PortalPayment,
    PortalPaymentCreate,
    PortalPaymentResult,
    PortalServiceRequest,
    PortalServiceRequestCreate,
    PortalVehicle,
    PortalVehicleHistory,
)
from app.portal.services import PortalService

router = APIRouter()


@router.get("/", response_model=PortalAccountSummary)
async def get_account_summary(
    current_user: User = Depends(require_permission(PermissionEnum.VEHICLES_READ)),
    portal: PortalService = Depends(get_portal_service),
):
    """What needs doing and what is owed, for the signed-in customer."""
    return await portal.get_summary()


@router.get("/vehicles", response_model=list[PortalVehicle])
async def list_my_vehicles(
    current_user: User = Depends(require_permission(PermissionEnum.VEHICLES_READ)),
    portal: PortalService = Depends(get_portal_service),
):
    """The vehicles on this account."""
    return await portal.list_vehicles()


@router.get("/vehicles/{vehicle_id}", response_model=PortalVehicleHistory)
async def get_vehicle_history(
    vehicle_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.VEHICLES_READ)),
    portal: PortalService = Depends(get_portal_service),
):
    """Everything that has happened to one vehicle."""
    return await portal.get_vehicle_history(vehicle_id)


@router.get("/appointments", response_model=list[PortalAppointment])
async def list_my_appointments(
    upcoming_only: bool = Query(False),
    current_user: User = Depends(require_permission(PermissionEnum.APPOINTMENTS_READ)),
    portal: PortalService = Depends(get_portal_service),
):
    """The appointments on this account."""
    return await portal.list_appointments(upcoming_only=upcoming_only)


@router.get("/service-requests", response_model=list[PortalServiceRequest])
async def list_my_service_requests(
    current_user: User = Depends(require_permission(PermissionEnum.SERVICE_REQUESTS_READ)),
    portal: PortalService = Depends(get_portal_service),
):
    """Requests this customer has sent."""
    return await portal.list_service_requests()


@router.get("/estimates", response_model=list[PortalEstimate])
async def list_my_estimates(
    open_only: bool = Query(False, description="Only estimates awaiting a decision"),
    current_user: User = Depends(require_permission(PermissionEnum.ESTIMATES_READ)),
    portal: PortalService = Depends(get_portal_service),
):
    """The estimates on this account."""
    return await portal.list_estimates(open_only=open_only)


@router.get("/estimates/{estimate_id}", response_model=PortalEstimate)
async def get_my_estimate(
    estimate_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.ESTIMATES_READ)),
    portal: PortalService = Depends(get_portal_service),
):
    """One estimate with its lines and the customer's decision on each."""
    return await portal.get_estimate_view(estimate_id)


@router.get("/estimates/{estimate_id}/document")
async def download_estimate_document(
    estimate_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.ESTIMATES_READ)),
    portal: PortalService = Depends(get_portal_service),
):
    """Download a printable copy of an estimate."""
    estimate = await portal.get_estimate(estimate_id)
    filename, html = render_estimate_document(estimate)
    return HTMLResponse(
        content=html,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/estimates/{estimate_id}/items/{item_id}/decision",
    response_model=PortalDecisionResult,
)
@audit(
    AuditAction.DECIDE, "estimate", id_param="estimate_id",
    summary="Recorded a customer decision on an estimate line",
)
async def decide_estimate_item(
    estimate_id: UUID,
    item_id: UUID,
    decision: ItemDecision,
    current_user: User = Depends(require_permission(PermissionEnum.ESTIMATES_APPROVE)),
    portal: PortalService = Depends(get_portal_service),
):
    """Approve or decline one line of an estimate.

    The same decision the customer can make at the counter, recorded through the
    same service: the portal adds a way in, not a second set of rules.
    """
    return await portal.decide_estimate_item(estimate_id, item_id, decision)


@router.get("/invoices", response_model=list[PortalInvoice])
async def list_my_invoices(
    unpaid_only: bool = Query(False),
    current_user: User = Depends(require_permission(PermissionEnum.INVOICES_READ)),
    portal: PortalService = Depends(get_portal_service),
):
    """The invoices on this account."""
    return await portal.list_invoices(unpaid_only=unpaid_only)


@router.get("/invoices/{invoice_id}", response_model=PortalInvoice)
async def get_my_invoice(
    invoice_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.INVOICES_READ)),
    portal: PortalService = Depends(get_portal_service),
):
    """One invoice, with what is still owed."""
    return await portal.get_invoice_view(invoice_id)


@router.get("/invoices/{invoice_id}/document")
async def download_invoice_document(
    invoice_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.INVOICES_READ)),
    portal: PortalService = Depends(get_portal_service),
):
    """Download a printable copy of an invoice."""
    invoice = await portal.get_invoice(invoice_id)
    filename, html = render_invoice_document(invoice)
    return HTMLResponse(
        content=html,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/appointments",
    response_model=PortalAppointment,
    status_code=status.HTTP_201_CREATED,
)
async def book_appointment(
    booking: PortalAppointmentCreate,
    current_user: User = Depends(require_permission(PermissionEnum.APPOINTMENTS_WRITE)),
    portal: PortalService = Depends(get_portal_service),
):
    """Ask for a slot, for a vehicle on this account.

    The booking goes into the shop's diary through its own service, so a slot
    that clashes with work already in the bay is refused here exactly as it
    would be at the counter. The customer picks the time they want; who takes the
    job is the shop's business.
    """
    return await portal.book_appointment(booking)


@router.post(
    "/service-requests",
    response_model=PortalServiceRequest,
    status_code=status.HTTP_201_CREATED,
)
async def create_service_request(
    request_data: PortalServiceRequestCreate,
    current_user: User = Depends(require_permission(PermissionEnum.SERVICE_REQUESTS_WRITE)),
    portal: PortalService = Depends(get_portal_service),
):
    """Ask the shop for work, from the account the token belongs to.

    The customer is derived from the token, so there is no customer id to
    supply and none to get wrong. This is the shop's own create path underneath,
    so a request filed here lands in the same queue as one taken at the counter.
    """
    return await portal.create_service_request(request_data)


@router.post(
    "/invoices/{invoice_id}/payments",
    response_model=PortalPaymentResult,
    status_code=status.HTTP_201_CREATED,
)
@audit(
    AuditAction.RECORD_PAYMENT, "payment", id_param="invoice_id",
    summary="Customer settled an invoice from the portal",
)
async def pay_invoice(
    invoice_id: UUID,
    payment: PortalPaymentCreate,
    current_user: User = Depends(require_permission(PermissionEnum.PAYMENTS_WRITE)),
    portal: PortalService = Depends(get_portal_service),
):
    """Pay part or all of one of this customer's own invoices.

    The shop's payment service does the work, including refusing a draft bill, a
    written-off one, or more than the balance — so a portal cannot settle an
    invoice the counter would have turned away. Somebody else's invoice is not
    found, never paid.
    """
    return await portal.pay_invoice(invoice_id, payment)


@router.get("/payments", response_model=list[PortalPayment])
async def list_my_payments(
    current_user: User = Depends(require_permission(PermissionEnum.PAYMENTS_READ)),
    portal: PortalService = Depends(get_portal_service),
):
    """Payments made against this account's invoices."""
    return await portal.list_payments()
