"""Customer portal business logic.

Reads the shop's own records and presents them to the signed-in customer, in the
customer's own words.

The whole module turns on one rule: **a customer sees their own data and nothing
else.** It is enforced in a single place rather than sprinkled across queries.
Every list is filtered by the customer id resolved from the signed-in user, and
every record named directly — an invoice id, an estimate id, a vehicle id — is
checked against that same customer before it is read. A record belonging to
somebody else is reported as **not found**, not as forbidden: a 403 would confirm
that the id exists, which is itself a leak.

The portal adds no tables and copies nothing. It is a view over the estimates,
repair orders, invoices and payments the shop already works with, so there is no
second copy of a number to fall out of step, and nothing here can drift from what
the counter sees.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.appointments.models import Appointment
from app.appointments.schemas import AppointmentCreate
from app.appointments.services import AppointmentService
from app.common.dates import today as shop_today
from app.common.exceptions import NotFoundError
from app.customers.models import Customer
from app.estimates.models import Estimate, EstimateItemStatus
from app.estimates.schemas import ItemDecision
from app.estimates.services import EstimateService
from app.inspections.models import Inspection
from app.invoices.models import Invoice
from app.invoices.services import InvoiceService
from app.payments.models import Payment
from app.payments.schemas import PaymentCreate
from app.payments.services import PaymentService
from app.portal.schemas import (
    PortalAccountSummary,
    PortalAppointment,
    PortalAppointmentCreate,
    PortalDecisionResult,
    PortalEstimate,
    PortalEstimateLine,
    PortalInspection,
    PortalInvoice,
    PortalPayment,
    PortalPaymentCreate,
    PortalPaymentResult,
    PortalRepairOrder,
    PortalServiceRequest,
    PortalServiceRequestCreate,
    PortalVehicle,
    PortalVehicleHistory,
)
from app.portal.statuses import (
    APPOINTMENT_STATUS,
    ESTIMATE_STATUS,
    INVOICE_STATUS,
    REPAIR_ORDER_STATUS,
    SERVICE_REQUEST_STATUS,
    CustomerStatus,
    describe,
)
from app.repair_orders.models import RepairOrder
from app.service_requests.models import ServiceRequest
from app.service_requests.schemas import ServiceRequestCreate
from app.service_requests.services import ServiceRequestService
from app.vehicles.models import Vehicle, VehicleStatus

logger = logging.getLogger("autofix.portal.services")

# Repair orders that mean the vehicle is still with the shop, or work is live.
OPEN_RO_STATUSES: frozenset[str] = frozenset(
    {
        "DRAFT",
        "APPROVED",
        "IN_PROGRESS",
        "ON_HOLD",
        "COMPLETED",
        "QC_PASSED",
    }
)

# Invoice statuses that still owe money.
OWING_INVOICE_STATUSES: frozenset[str] = frozenset({"ISSUED", "PARTIALLY_PAID"})

# The customer's words for the state of a vehicle in the shop's care.
VEHICLE_STATUS_VIEW: dict[str, tuple[str, str]] = {
    VehicleStatus.ACTIVE.value: ("With you", "This vehicle is not currently booked in."),
    VehicleStatus.IN_SHOP.value: ("In the shop", "This vehicle is currently with us."),
    VehicleStatus.ARCHIVED.value: ("Archived", "This vehicle is no longer serviced by us."),
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PortalService:
    """Everything the customer portal shows, scoped to one customer."""

    def __init__(self, db: AsyncSession, customer: Customer):
        self.db = db
        self.customer = customer
        self.estimates = EstimateService(db)
        self.invoices = InvoiceService(db)
        self.payments = PaymentService(db)
        self.service_requests = ServiceRequestService(db)
        self.appointments = AppointmentService(db)

    @property
    def customer_id(self) -> str:
        return str(self.customer.id)

    # --- ownership ----------------------------------------------------------

    async def get_vehicle(self, vehicle_id: UUID | str) -> Vehicle:
        """Load one of the customer's own vehicles."""
        result = await self.db.execute(
            select(Vehicle).where(
                Vehicle.id == str(vehicle_id),
                Vehicle.customer_id == self.customer_id,
            )
        )
        vehicle = result.scalar_one_or_none()
        if not vehicle:
            raise NotFoundError(f"Vehicle {vehicle_id} not found")
        return vehicle

    async def get_estimate(self, estimate_id: UUID | str) -> Estimate:
        result = await self.db.execute(
            select(Estimate)
            .options(selectinload(Estimate.items))
            .where(
                Estimate.id == str(estimate_id),
                Estimate.customer_id == self.customer_id,
            )
            .execution_options(populate_existing=True)
        )
        estimate = result.scalar_one_or_none()
        if not estimate:
            raise NotFoundError(f"Estimate {estimate_id} not found")
        return estimate

    async def get_invoice(self, invoice_id: UUID | str) -> Invoice:
        result = await self.db.execute(
            select(Invoice)
            .options(selectinload(Invoice.items))
            .where(
                Invoice.id == str(invoice_id),
                Invoice.customer_id == self.customer_id,
            )
            .execution_options(populate_existing=True)
        )
        invoice = result.scalar_one_or_none()
        if not invoice:
            raise NotFoundError(f"Invoice {invoice_id} not found")
        return invoice

    async def get_estimate_view(self, estimate_id: UUID | str) -> PortalEstimate:
        """One estimate, owned by this customer, in customer-facing form."""
        return self._estimate_view(await self.get_estimate(estimate_id))

    async def get_invoice_view(self, invoice_id: UUID | str) -> PortalInvoice:
        """One invoice, owned by this customer, in customer-facing form."""
        return self._invoice_view(await self.get_invoice(invoice_id))

    # --- account ------------------------------------------------------------

    async def get_summary(self) -> PortalAccountSummary:
        """The portal's landing view: what needs doing, and what is owed.

        Every figure is scoped to this customer in the query itself. Summing in
        Python from an unscoped list would be a single missing ``where`` away from
        publishing the shop's takings.
        """
        vehicle_count = (
            await self.db.execute(
                select(func.count(Vehicle.id)).where(
                    Vehicle.customer_id == self.customer_id
                )
            )
        ).scalar_one()

        balance_rows = (
            await self.db.execute(
                select(
                    func.coalesce(func.sum(Invoice.total), 0.0),
                    func.coalesce(func.sum(Invoice.amount_paid), 0.0),
                ).where(
                    Invoice.customer_id == self.customer_id,
                    Invoice.status.in_(OWING_INVOICE_STATUSES),
                )
            )
        ).one()
        total, paid = balance_rows
        open_balance = round(float(total or 0.0) - float(paid or 0.0), 2)

        overdue_balance = 0.0
        overdue_invoices = (
            await self.db.execute(
                select(Invoice).where(
                    Invoice.customer_id == self.customer_id,
                    Invoice.status.in_(OWING_INVOICE_STATUSES),
                    Invoice.due_date.is_not(None),
                    Invoice.due_date < shop_today(),
                )
            )
        ).scalars().all()
        for invoice in overdue_invoices:
            overdue_balance = round(overdue_balance + invoice.balance, 2)

        awaiting_approval = (
            await self.db.execute(
                select(func.count(Estimate.id)).where(
                    Estimate.customer_id == self.customer_id,
                    Estimate.status.in_(["SENT", "PARTIALLY_APPROVED"]),
                )
            )
        ).scalar_one()

        awaiting_payment = (
            await self.db.execute(
                select(func.count(Invoice.id)).where(
                    Invoice.customer_id == self.customer_id,
                    Invoice.status.in_(OWING_INVOICE_STATUSES),
                )
            )
        ).scalar_one()

        ready_for_pickup = (
            await self.db.execute(
                select(func.count(RepairOrder.id)).where(
                    RepairOrder.customer_id == self.customer_id,
                    RepairOrder.status == "QC_PASSED",
                )
            )
        ).scalar_one()

        upcoming = (
            await self.db.execute(
                select(Appointment)
                .where(
                    Appointment.customer_id == self.customer_id,
                    Appointment.status.in_(["REQUESTED", "CONFIRMED"]),
                )
                .order_by(Appointment.scheduled_start.asc().nulls_last())
                .limit(1)
            )
        ).scalar_one_or_none()

        return PortalAccountSummary(
            customer_id=self.customer.id,
            full_name=self.customer.full_name,
            email=self.customer.email,
            phone=self.customer.phone,
            vehicle_count=int(vehicle_count or 0),
            open_balance=max(open_balance, 0.0),
            overdue_balance=max(overdue_balance, 0.0),
            awaiting_approval=int(awaiting_approval or 0),
            awaiting_payment=int(awaiting_payment or 0),
            ready_for_pickup=int(ready_for_pickup or 0),
            next_appointment=(
                self._appointment_view(upcoming, await self._vehicle_labels())
                if upcoming is not None
                else None
            ),
        )

    # --- vehicles -----------------------------------------------------------

    async def list_vehicles(self) -> list[PortalVehicle]:
        """The customer's vehicles, most recently serviced first."""
        result = await self.db.execute(
            select(Vehicle)
            .where(Vehicle.customer_id == self.customer_id)
            .order_by(Vehicle.created_at.desc())
        )
        return [self._vehicle_view(v) for v in result.scalars().all()]

    async def get_vehicle_history(self, vehicle_id: UUID | str) -> PortalVehicleHistory:
        """Everything that has happened to one of the customer's vehicles."""
        vehicle = await self.get_vehicle(vehicle_id)

        orders = list(
            (
                await self.db.execute(
                    select(RepairOrder)
                    .where(
                        RepairOrder.vehicle_id == str(vehicle.id),
                        RepairOrder.customer_id == self.customer_id,
                    )
                    .order_by(RepairOrder.created_at.desc())
                )
            ).scalars().all()
        )
        inspections = list(
            (
                await self.db.execute(
                    select(Inspection)
                    .options(selectinload(Inspection.items))
                    .where(
                        Inspection.vehicle_id == str(vehicle.id),
                        Inspection.customer_id == self.customer_id,
                    )
                    .order_by(Inspection.created_at.desc())
                )
            ).scalars().all()
        )
        invoices = list(
            (
                await self.db.execute(
                    select(Invoice)
                    .where(
                        Invoice.vehicle_id == str(vehicle.id),
                        Invoice.customer_id == self.customer_id,
                    )
                    .order_by(Invoice.invoice_date.desc(), Invoice.created_at.desc())
                )
            ).scalars().all()
        )
        requests = list(
            (
                await self.db.execute(
                    select(ServiceRequest)
                    .where(
                        ServiceRequest.vehicle_id == str(vehicle.id),
                        ServiceRequest.customer_id == self.customer_id,
                    )
                    .order_by(ServiceRequest.created_at.desc())
                )
            ).scalars().all()
        )

        # Spend is counted from settled invoices only: a draft or a written-off
        # bill is not money the customer spent, and an unpaid one is not history
        # they have completed.
        total_spent = round(
            sum(
                float(i.total)
                for i in invoices
                if i.status in ("PAID", "PARTIALLY_PAID")
            ),
            2,
        )

        return PortalVehicleHistory(
            vehicle=self._vehicle_view(vehicle),
            repair_orders=[self._repair_order_view(o) for o in orders],
            inspections=[self._inspection_view(i) for i in inspections],
            invoices=[self._invoice_view(i) for i in invoices],
            service_requests=[self._service_request_view(r) for r in requests],
            total_spent=total_spent,
            visit_count=len(orders),
        )

    # --- appointments and requests -----------------------------------------

    async def list_appointments(
        self, *, upcoming_only: bool = False
    ) -> list[PortalAppointment]:
        """The customer's appointments, soonest first."""
        stmt = select(Appointment).where(Appointment.customer_id == self.customer_id)
        if upcoming_only:
            stmt = stmt.where(
                Appointment.status.in_(["REQUESTED", "CONFIRMED", "CHECKED_IN", "IN_SERVICE"])
            )
        stmt = stmt.order_by(
            Appointment.scheduled_start.asc().nulls_last(), Appointment.created_at.desc()
        )
        result = await self.db.execute(stmt)
        labels = await self._vehicle_labels()
        return [self._appointment_view(a, labels) for a in result.scalars().all()]

    async def book_appointment(
        self, data: PortalAppointmentCreate
    ) -> PortalAppointment:
        """Book a visit on this customer's own account.

        The vehicle is checked against the account first, so a booking cannot be
        made against somebody else's car, and the diary's own clash rules apply
        because this is the shop's booking service doing the writing. A customer
        asking for a slot the shop has already given away is told so at the point
        of asking.
        """
        await self.get_vehicle(data.vehicle_id)

        appointment = await self.appointments.create_appointment(
            AppointmentCreate(
                customer_id=self.customer.id,
                vehicle_id=data.vehicle_id,
                service_type=data.service_type,
                scheduled_start=data.scheduled_start,
                duration_minutes=data.duration_minutes,
                customer_concern=data.customer_concern,
            )
        )
        return self._appointment_view(appointment, await self._vehicle_labels())

    async def list_service_requests(self) -> list[PortalServiceRequest]:
        result = await self.db.execute(
            select(ServiceRequest)
            .where(ServiceRequest.customer_id == self.customer_id)
            .order_by(ServiceRequest.created_at.desc())
        )
        return [self._service_request_view(r) for r in result.scalars().all()]

    async def create_service_request(
        self, data: PortalServiceRequestCreate
    ) -> PortalServiceRequest:
        """File a request against this customer's own account.

        The customer is taken from the token and the vehicle, if named, is checked
        against this account first — through :meth:`get_vehicle`, so somebody
        else's car is a 404 rather than a request quietly filed against it.

        The write itself is the shop's own service, unchanged. The portal adds a
        way in; it does not get a second, laxer set of rules for what a valid
        request is.
        """
        if data.vehicle_id is not None:
            await self.get_vehicle(data.vehicle_id)

        request = await self.service_requests.create_request(
            ServiceRequestCreate(
                customer_id=self.customer.id,
                vehicle_id=data.vehicle_id,
                title=data.title,
                description=data.description,
                priority=data.priority,
            )
        )
        return self._service_request_view(request)

    # --- money --------------------------------------------------------------

    async def list_estimates(self, *, open_only: bool = False) -> list[PortalEstimate]:
        """The customer's estimates, newest first."""
        stmt = select(Estimate).options(selectinload(Estimate.items)).where(
            Estimate.customer_id == self.customer_id
        )
        if open_only:
            # Only the ones actually waiting on a decision. An expired estimate
            # is not something the customer can act on, so listing it as "open"
            # would be asking for a decision that will be refused — the customer
            # would tap approve and be told no. The expiry is re-applied here in
            # SQL rather than filtered in Python, so the count and the list agree.
            stmt = stmt.where(
                Estimate.status.in_(["SENT", "PARTIALLY_APPROVED"]),
                or_(
                    Estimate.valid_until.is_(None),
                    Estimate.valid_until >= shop_today(),
                ),
            )
        stmt = stmt.order_by(Estimate.created_at.desc())
        result = await self.db.execute(stmt)
        return [self._estimate_view(e) for e in result.scalars().all()]

    async def list_invoices(
        self, *, unpaid_only: bool = False
    ) -> list[PortalInvoice]:
        """The customer's invoices, newest first."""
        stmt = select(Invoice).where(Invoice.customer_id == self.customer_id)
        if unpaid_only:
            stmt = stmt.where(Invoice.status.in_(OWING_INVOICE_STATUSES))
        stmt = stmt.order_by(Invoice.invoice_date.desc(), Invoice.created_at.desc())
        result = await self.db.execute(stmt)
        return [self._invoice_view(i) for i in result.scalars().all()]

    async def list_payments(self) -> list[PortalPayment]:
        """Payments the customer has made, most recent first.

        Joins through the invoice, so the list is scoped by ownership in the query
        rather than by filtering afterwards.
        """
        result = await self.db.execute(
            select(Payment, Invoice)
            .join(Invoice, Payment.invoice_id == Invoice.id)
            .where(Invoice.customer_id == self.customer_id)
            .order_by(Payment.payment_date.desc(), Payment.created_at.desc())
        )
        return [
            PortalPayment(
                id=payment.id,
                invoice_id=invoice.id,
                invoice_number=invoice.invoice_number,
                amount=float(payment.amount),
                method=payment.method,
                status=payment.status,
                payment_date=payment.payment_date,
                reference=payment.reference,
            )
            for payment, invoice in result.all()
        ]

    async def decide_estimate_item(
        self, estimate_id: UUID | str, item_id: UUID | str, decision: ItemDecision
    ) -> PortalDecisionResult:
        """Record the customer's decision on one estimate line.

        Delegates to the estimate service so the portal cannot grow a second,
        laxer version of the approval rules, and so the shop sees exactly the same
        workflow whether the decision arrives from the portal, the counter or the
        phone. The estimate is loaded through :meth:`get_estimate` first, which is
        what makes it the *customer's* estimate being decided.
        """
        estimate = await self.get_estimate(estimate_id)
        await self.estimates.decide_item(estimate.id, item_id, decision)

        refreshed = await self.get_estimate(estimate.id)
        remaining = sum(
            1
            for i in refreshed.items
            if i.status == EstimateItemStatus.PENDING.value
        )
        return PortalDecisionResult(
            estimate_id=refreshed.id,
            estimate_number=refreshed.estimate_number,
            item_id=item_id,
            item_status=decision.decision,
            estimate_status=refreshed.status,
            estimate_status_view=describe(refreshed.status, ESTIMATE_STATUS),
            approved_total=refreshed.approved_total,
            total=float(refreshed.total),
            remaining_to_decide=remaining,
        )

    async def pay_invoice(
        self, invoice_id: UUID | str, data: PortalPaymentCreate
    ) -> PortalPaymentResult:
        """Settle part or all of one of this customer's own invoices.

        The invoice is loaded through :meth:`get_invoice` first, so an invoice
        belonging to somebody else is a 404 and never becomes a payment. The
        amount, the method rules and the effect on the balance all come from the
        shop's payment service, so a bill cannot be settled twice over or paid
        with an amount the shop would have refused at the counter.

        The payment is recorded against the *customer's own user id*, not the
        staff member who happens to be at the desk: money that arrived over the
        portal was not taken by an employee, and the audit trail should say so.
        """
        invoice = await self.get_invoice(invoice_id)

        payment = await self.payments.record_payment(
            PaymentCreate(
                invoice_id=invoice.id,
                amount=data.amount,
                method=data.method,
                reference=data.reference,
                notes=data.notes,
            ),
            recorded_by_id=self.customer.user_id,
        )

        refreshed = await self.get_invoice(invoice.id)
        return PortalPaymentResult(
            payment=PortalPayment(
                id=payment.id,
                invoice_id=invoice.id,
                invoice_number=invoice.invoice_number,
                amount=float(payment.amount),
                method=payment.method,
                status=payment.status,
                payment_date=payment.payment_date,
                reference=payment.reference,
            ),
            invoice_status=refreshed.status,
            invoice_status_view=describe(refreshed.status, INVOICE_STATUS),
            invoice_balance=refreshed.balance,
        )

    # --- views --------------------------------------------------------------

    @staticmethod
    def _vehicle_view(vehicle: Vehicle) -> PortalVehicle:
        label, detail = VEHICLE_STATUS_VIEW.get(
            vehicle.status, (vehicle.status.title(), "")
        )
        return PortalVehicle(
            id=vehicle.id,
            make=vehicle.make,
            model=vehicle.model,
            year=vehicle.year,
            color=vehicle.color,
            license_plate=vehicle.license_plate,
            vin=vehicle.vin,
            mileage=vehicle.mileage,
            status=vehicle.status,
            status_view=CustomerStatus(
                status=vehicle.status, label=label, detail=detail
            ),
        )

    @staticmethod
    def _vehicle_label(vehicle: Vehicle) -> str:
        parts = [vehicle.make, vehicle.model]
        if vehicle.license_plate:
            parts.append(f"({vehicle.license_plate})")
        return " ".join(p for p in parts if p)

    async def _vehicle_labels(self) -> dict[str, str]:
        """Short labels for the customer's vehicles, keyed by id.

        Loaded once per request rather than per appointment: an appointment has no
        vehicle relationship, and touching one per row would be a lazy load per
        row inside the async greenlet — or, worse, a reason for somebody to
        "fix" it with a loop of queries later.
        """
        result = await self.db.execute(
            select(Vehicle).where(Vehicle.customer_id == self.customer_id)
        )
        return {
            str(v.id): self._vehicle_label(v) for v in result.scalars().all()
        }

    def _appointment_view(
        self, appointment: Appointment, labels: dict[str, str] | None = None
    ) -> PortalAppointment:
        label = (labels or {}).get(
            str(appointment.vehicle_id), "Your vehicle"
        )
        return PortalAppointment(
            id=appointment.id,
            vehicle_id=appointment.vehicle_id,
            vehicle_label=label,
            service_type=appointment.service_type,
            status=appointment.status,
            status_view=describe(appointment.status, APPOINTMENT_STATUS),
            scheduled_start=appointment.scheduled_start,
            scheduled_end=appointment.scheduled_end,
            # The shop's bay plan is its own business, and the technician's name
            # is not something a customer portal needs to publish.
            bay=None,
            notes=appointment.customer_concern,
            created_at=appointment.created_at,
        )

    def _estimate_view(self, estimate: Estimate) -> PortalEstimate:
        can_decide = estimate.is_open_for_decision
        return PortalEstimate(
            id=estimate.id,
            estimate_number=estimate.estimate_number,
            vehicle_id=estimate.vehicle_id,
            status=estimate.status,
            status_view=describe(estimate.status, ESTIMATE_STATUS),
            valid_until=estimate.valid_until,
            is_expired=estimate.is_expired,
            total=float(estimate.total),
            approved_total=estimate.approved_total,
            notes=estimate.notes,
            customer_notes=estimate.customer_notes,
            decline_reason=estimate.decline_reason,
            items=[
                PortalEstimateLine(
                    id=item.id,
                    item_type=item.item_type,
                    description=item.description,
                    quantity=float(item.quantity),
                    unit_price=float(item.unit_price),
                    line_total=float(item.line_total),
                    status=item.status,
                    is_optional=item.is_optional,
                    can_decide=can_decide
                    and item.status == EstimateItemStatus.PENDING.value,
                )
                for item in estimate.items
            ],
            created_at=estimate.created_at,
        )

    @staticmethod
    def _invoice_view(invoice: Invoice) -> PortalInvoice:
        return PortalInvoice(
            id=invoice.id,
            invoice_number=invoice.invoice_number,
            vehicle_id=invoice.vehicle_id,
            status=invoice.status,
            status_view=describe(invoice.status, INVOICE_STATUS),
            invoice_date=invoice.invoice_date,
            due_date=invoice.due_date,
            subtotal=float(invoice.subtotal),
            discount_amount=float(invoice.discount_amount),
            tax_amount=float(invoice.tax_amount),
            total=float(invoice.total),
            amount_paid=float(invoice.amount_paid),
            balance=invoice.balance,
            is_overdue=invoice.is_overdue,
            days_overdue=invoice.days_overdue,
            notes=invoice.customer_notes or invoice.notes,
        )

    @staticmethod
    def _repair_order_view(ro: RepairOrder) -> PortalRepairOrder:
        return PortalRepairOrder(
            id=ro.id,
            ro_number=ro.ro_number,
            vehicle_id=ro.vehicle_id,
            status=ro.status,
            status_view=describe(ro.status, REPAIR_ORDER_STATUS),
            promised_at=ro.promised_at,
            started_at=ro.started_at,
            completed_at=ro.completed_at,
            delivered_at=ro.delivered_at,
            customer_notes=ro.customer_notes,
        )

    @staticmethod
    def _inspection_view(inspection: Inspection) -> PortalInspection:
        return PortalInspection(
            id=inspection.id,
            vehicle_id=inspection.vehicle_id,
            status=inspection.status,
            overall_condition=inspection.overall_condition,
            mileage=inspection.mileage,
            created_at=inspection.created_at,
        )

    @staticmethod
    def _service_request_view(request: ServiceRequest) -> PortalServiceRequest:
        return PortalServiceRequest(
            id=request.id,
            vehicle_id=request.vehicle_id,
            title=request.title,
            description=request.description,
            priority=request.priority,
            status=request.status,
            status_view=describe(request.status, SERVICE_REQUEST_STATUS),
            created_at=request.created_at,
        )


__all__ = ["OPEN_RO_STATUSES", "OWING_INVOICE_STATUSES", "PortalService"]
