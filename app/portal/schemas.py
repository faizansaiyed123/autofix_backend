"""Pydantic schemas for the customer portal.

Deliberately narrower than the shop's own schemas. A portal response answers
"what is happening and what do I owe", so it carries no internal identifiers that
would let a caller walk sideways into shop data, no cost figures, and no
technician or bay names. Money is present because the customer owes it; the
estimate's *proposed* money is present because they are being asked to approve
it. Anything else is the shop's business.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from pydantic import Field, field_validator, model_validator

from app.appointments.models import ServiceType
from app.common.schemas import BaseSchema
from app.payments.models import REFERENCED_PAYMENT_METHODS, PaymentMethod
from app.portal.statuses import CustomerStatus
from app.service_requests.models import ServiceRequestPriority


class PortalVehicle(BaseSchema):
    """One of the customer's vehicles."""

    id: uuid.UUID
    make: str
    model: str
    year: int | None = None
    color: str | None = None
    license_plate: str | None = None
    vin: str | None = None
    mileage: int | None = None
    status: str
    # The vehicle's in-shop state in the customer's words, not the shop's code.
    status_view: CustomerStatus


class PortalAppointment(BaseSchema):
    """A booked visit, in the customer's words."""

    id: uuid.UUID
    vehicle_id: uuid.UUID
    vehicle_label: str
    service_type: str
    status: str
    status_view: CustomerStatus
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    bay: str | None = None
    notes: str | None = None
    created_at: datetime


class PortalEstimateLine(BaseSchema):
    """One line on an estimate the customer is being asked to decide on."""

    id: uuid.UUID
    item_type: str
    description: str
    quantity: float
    unit_price: float
    line_total: float
    status: str
    is_optional: bool = False
    can_decide: bool = False


class PortalEstimate(BaseSchema):
    """An estimate as the customer sees it."""

    id: uuid.UUID
    estimate_number: str
    vehicle_id: uuid.UUID
    status: str
    status_view: CustomerStatus
    valid_until: date | None = None
    is_expired: bool = False
    # The figure the customer is being asked to agree to. Only the approved lines
    # count, because a declined line is not going on the bill.
    total: float
    approved_total: float
    notes: str | None = None
    customer_notes: str | None = None
    decline_reason: str | None = None
    items: list[PortalEstimateLine] = Field(default_factory=list)
    created_at: datetime


class PortalInvoice(BaseSchema):
    """An invoice as the customer sees it."""

    id: uuid.UUID
    invoice_number: str
    vehicle_id: uuid.UUID
    status: str
    status_view: CustomerStatus
    invoice_date: date
    due_date: date | None = None
    subtotal: float
    discount_amount: float
    tax_amount: float
    total: float
    amount_paid: float
    balance: float
    is_overdue: bool
    days_overdue: int
    notes: str | None = None


class PortalPayment(BaseSchema):
    """A payment the customer has made."""

    id: uuid.UUID
    invoice_id: uuid.UUID
    invoice_number: str
    amount: float
    method: str
    status: str
    payment_date: date
    reference: str | None = None


class PortalRepairOrder(BaseSchema):
    """A visit to the shop, in the customer's words."""

    id: uuid.UUID
    ro_number: str
    vehicle_id: uuid.UUID
    status: str
    status_view: CustomerStatus
    promised_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    delivered_at: datetime | None = None
    customer_notes: str | None = None


class PortalServiceRequest(BaseSchema):
    """A request the customer has sent."""

    id: uuid.UUID
    vehicle_id: uuid.UUID | None = None
    title: str
    description: str | None = None
    priority: str
    status: str
    status_view: CustomerStatus
    created_at: datetime


class PortalAppointmentCreate(BaseSchema):
    """A booking request from a customer.

    No ``customer_id``, no ``bay``, no ``technician_id``: the account comes from
    the token, and who the shop puts in the bay is the shop's decision, not
    something a customer chooses. A slot the diary cannot hold is refused by the
    shop's own booking rules.
    """

    vehicle_id: uuid.UUID
    service_type: str = ServiceType.OTHER.value
    scheduled_start: datetime
    duration_minutes: int = Field(60, ge=5, le=1440)
    customer_concern: str | None = Field(None, max_length=2000)

    @field_validator("service_type")
    @classmethod
    def _check_service_type(cls, v: str) -> str:
        try:
            return ServiceType(v.upper()).value
        except ValueError:
            allowed = ", ".join(s.value for s in ServiceType)
            raise ValueError(f"Invalid service type '{v}'. Allowed: {allowed}")

    @field_validator("scheduled_start")
    @classmethod
    def _normalise_start(cls, v: datetime) -> datetime:
        """A naive time is read as UTC, exactly as the shop's schema reads it."""
        return v if v.tzinfo is not None else v.replace(tzinfo=UTC)


class PortalServiceRequestCreate(BaseSchema):
    """A request the customer is filing from the portal.

    There is no ``customer_id`` here and there is no way to add one: the account
    is derived from the token. A body that could name a customer would be a body
    that could file a request against somebody else's car.
    """

    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(None, max_length=5000)
    priority: str = ServiceRequestPriority.STANDARD.value
    vehicle_id: uuid.UUID | None = None


class PortalPaymentCreate(BaseSchema):
    """Money the customer is sending against one of their own invoices.

    The invoice is named in the path, never in the body, for the same reason the
    request has no customer id: the caller chooses the URL, and the URL is
    checked against the account the token belongs to.
    """

    amount: float = Field(..., gt=0, le=10_000_000)
    # How the customer says they paid. The shop confirms it at the counter; the
    # portal is not a payment processor and does not pretend to be one.
    method: str = PaymentMethod.CARD.value
    reference: str | None = Field(None, max_length=100)
    notes: str | None = Field(None, max_length=1000)

    @field_validator("method")
    @classmethod
    def _check_method(cls, v: str) -> str:
        try:
            return PaymentMethod(str(v).upper()).value
        except ValueError:
            allowed = ", ".join(m.value for m in PaymentMethod)
            raise ValueError(f"Invalid payment method '{v}'. Allowed: {allowed}")

    @model_validator(mode="after")
    def _check_reference_present(self) -> PortalPaymentCreate:
        """The shop's rule, restated at the portal's own edge.

        A transfer or a cheque with no reference cannot be matched to a bank
        statement at month end. The shop's payment service refuses it anyway;
        checking here means the customer is told at the form rather than after,
        and that the refusal is a 422 about their input instead of a failure
        raised from inside a service.
        """
        if self.method in REFERENCED_PAYMENT_METHODS and not (self.reference or "").strip():
            raise ValueError(
                f"a {self.method} payment requires a reference so it can be matched "
                "to the bank statement"
            )
        return self


class PortalPaymentResult(BaseSchema):
    """A payment the customer just made, and what is left on the bill.

    The new balance is in the response because the next thing a customer wants
    to know after paying is whether they still owe anything — and re-fetching
    the invoice to find out would be the shop's API doing the portal's job.
    """

    payment: PortalPayment
    invoice_status: str
    invoice_status_view: CustomerStatus
    invoice_balance: float


class PortalInspection(BaseSchema):
    """A vehicle health check, summarised.

    Only the verdict and the recommendations: the shop's per-item measurements
    are its working notes, not a customer document.
    """

    id: uuid.UUID
    vehicle_id: uuid.UUID
    status: str
    # GREEN / YELLOW / RED, derived by the shop from the item severities.
    overall_condition: str
    mileage: int | None = None
    created_at: datetime


class PortalVehicleHistory(BaseSchema):
    """Everything that has happened to one vehicle, newest first."""

    vehicle: PortalVehicle
    repair_orders: list[PortalRepairOrder] = Field(default_factory=list)
    inspections: list[PortalInspection] = Field(default_factory=list)
    invoices: list[PortalInvoice] = Field(default_factory=list)
    service_requests: list[PortalServiceRequest] = Field(default_factory=list)
    total_spent: float = 0.0
    visit_count: int = 0


class PortalAccountSummary(BaseSchema):
    """The portal's landing view: what needs doing, and what is owed."""

    customer_id: uuid.UUID
    full_name: str
    email: str | None = None
    phone: str | None = None
    vehicle_count: int
    open_balance: float
    overdue_balance: float
    awaiting_approval: int
    awaiting_payment: int
    ready_for_pickup: int
    next_appointment: PortalAppointment | None = None


class PortalDecisionResult(BaseSchema):
    """The outcome of a customer's decision on one estimate line."""

    estimate_id: uuid.UUID
    estimate_number: str
    item_id: uuid.UUID
    item_status: str
    estimate_status: str
    estimate_status_view: CustomerStatus
    approved_total: float
    total: float
    remaining_to_decide: int


__all__ = [
    "PortalAccountSummary",
    "PortalAppointment",
    "PortalAppointmentCreate",
    "PortalDecisionResult",
    "PortalEstimate",
    "PortalEstimateLine",
    "PortalInspection",
    "PortalInvoice",
    "PortalPayment",
    "PortalPaymentCreate",
    "PortalPaymentResult",
    "PortalRepairOrder",
    "PortalServiceRequest",
    "PortalServiceRequestCreate",
    "PortalVehicle",
    "PortalVehicleHistory",
]
