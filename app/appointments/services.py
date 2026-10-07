"""Appointment scheduling business logic.

Covers the appointment lifecycle, conflict detection, and the day / week /
month calendar views.

Conflict rules
--------------
A prospective slot clashes with an existing appointment when their time
windows overlap *and* they compete for the same finite resource:

* the same technician  - a person cannot be in two places at once
* the same bay         - a physical lift holds one vehicle
* the same vehicle     - one car cannot be serviced twice simultaneously

Appointments that are CANCELLED, NO_SHOW or COMPLETED have released their
slot and never block a new booking. Two appointments with no technician, no
bay and different vehicles do not conflict: the shop can serve several
customers at once, and capacity limits are a separate concern from
double-booking a specific resource.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.appointments.models import (
    RELEASED_STATUSES,
    Appointment,
    AppointmentStatus,
)
from app.appointments.schemas import (
    AppointmentCreate,
    AppointmentFromServiceRequest,
    AppointmentRead,
    AppointmentStatusUpdate,
    AppointmentUpdate,
    CalendarDay,
    CalendarResponse,
    ConflictCheckRequest,
    ConflictCheckResponse,
    ConflictDetail,
)
from app.common.exceptions import BusinessRuleError, ConflictError, NotFoundError

logger = logging.getLogger("autofix.appointments.services")

VALID_STATUS_TRANSITIONS: dict[str, list[str]] = {
    AppointmentStatus.REQUESTED.value: [
        AppointmentStatus.CONFIRMED.value,
        AppointmentStatus.CANCELLED.value,
    ],
    AppointmentStatus.CONFIRMED.value: [
        AppointmentStatus.CHECKED_IN.value,
        AppointmentStatus.CANCELLED.value,
        AppointmentStatus.NO_SHOW.value,
    ],
    AppointmentStatus.CHECKED_IN.value: [
        AppointmentStatus.IN_SERVICE.value,
        AppointmentStatus.CANCELLED.value,
    ],
    AppointmentStatus.IN_SERVICE.value: [
        AppointmentStatus.COMPLETED.value,
        AppointmentStatus.CANCELLED.value,
    ],
    AppointmentStatus.COMPLETED.value: [],
    AppointmentStatus.CANCELLED.value: [],
    AppointmentStatus.NO_SHOW.value: [],
}

# Once the vehicle is in the shop, moving the booking around the calendar no
# longer means anything.
_UNRESCHEDULABLE = {
    AppointmentStatus.CHECKED_IN.value,
    AppointmentStatus.IN_SERVICE.value,
    AppointmentStatus.COMPLETED.value,
    AppointmentStatus.CANCELLED.value,
    AppointmentStatus.NO_SHOW.value,
}

CALENDAR_VIEWS = ("day", "week", "month")


def _validate_status_transition(current: str, new: str) -> None:
    """Raise unless the appointment status transition is allowed."""
    allowed = VALID_STATUS_TRANSITIONS.get(current, [])
    if new not in allowed:
        raise BusinessRuleError(
            f"Cannot transition appointment from '{current}' to '{new}'. "
            f"Allowed: {allowed or 'none (terminal state)'}"
        )


def _as_aware(value: datetime) -> datetime:
    """Treat a naive datetime as UTC, so comparisons never raise."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class AppointmentService:
    """Service for appointment scheduling operations."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # --- reads --------------------------------------------------------------

    async def get_by_id(self, appointment_id: UUID | str) -> Appointment:
        """Get an appointment by ID."""
        result = await self.db.execute(
            select(Appointment)
            .where(Appointment.id == str(appointment_id))
            .execution_options(populate_existing=True)
        )
        appointment = result.scalar_one_or_none()
        if not appointment:
            raise NotFoundError(f"Appointment with id {appointment_id} not found")
        return appointment

    async def list_appointments(
        self,
        *,
        page: int = 1,
        size: int = 20,
        status: str | None = None,
        customer_id: UUID | str | None = None,
        vehicle_id: UUID | str | None = None,
        technician_id: UUID | str | None = None,
        advisor_id: UUID | str | None = None,
        bay: str | None = None,
        start_from: datetime | None = None,
        start_to: datetime | None = None,
    ) -> tuple[list[Appointment], int]:
        """List appointments with filtering and pagination."""
        stmt = select(Appointment)
        count_stmt = select(func.count()).select_from(Appointment)

        conditions = []
        if status:
            conditions.append(Appointment.status == status)
        if customer_id:
            conditions.append(Appointment.customer_id == str(customer_id))
        if vehicle_id:
            conditions.append(Appointment.vehicle_id == str(vehicle_id))
        if technician_id:
            conditions.append(Appointment.technician_id == str(technician_id))
        if advisor_id:
            conditions.append(Appointment.advisor_id == str(advisor_id))
        if bay:
            conditions.append(Appointment.bay == bay)
        if start_from:
            conditions.append(Appointment.scheduled_start >= _as_aware(start_from))
        if start_to:
            conditions.append(Appointment.scheduled_start < _as_aware(start_to))

        for condition in conditions:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        total = (await self.db.execute(count_stmt)).scalar_one()

        stmt = (
            stmt.order_by(Appointment.scheduled_start.asc())
            .offset((page - 1) * size)
            .limit(size)
        )
        appointments = list((await self.db.execute(stmt)).scalars().all())
        return appointments, total

    # --- conflict detection -------------------------------------------------

    async def find_conflicts(self, request: ConflictCheckRequest) -> list[ConflictDetail]:
        """Return every existing appointment that clashes with the given slot.

        Only appointments competing for the same technician, bay or vehicle
        are considered, and only those still holding their slot.
        """
        start = _as_aware(request.scheduled_start)
        end = start + timedelta(minutes=request.duration_minutes)

        # Narrow the scan to appointments that could plausibly overlap before
        # doing the precise comparison in Python.
        resource_filters = []
        if request.technician_id:
            resource_filters.append(Appointment.technician_id == str(request.technician_id))
        if request.bay:
            resource_filters.append(Appointment.bay == request.bay)
        if request.vehicle_id:
            resource_filters.append(Appointment.vehicle_id == str(request.vehicle_id))

        if not resource_filters:
            # Nothing exclusive is being reserved, so nothing can clash.
            return []

        stmt = (
            select(Appointment)
            .where(Appointment.status.not_in(tuple(RELEASED_STATUSES)))
            .where(or_(*resource_filters))
            # A candidate must start before our window ends. The symmetric
            # end-boundary check needs the duration, so it happens below.
            .where(Appointment.scheduled_start < end)
            .where(
                Appointment.scheduled_start
                > start - timedelta(minutes=1440)
            )
        )
        if request.exclude_appointment_id:
            stmt = stmt.where(Appointment.id != str(request.exclude_appointment_id))

        candidates = (await self.db.execute(stmt)).scalars().all()

        conflicts: list[ConflictDetail] = []
        for candidate in candidates:
            if not candidate.overlaps(start, end):
                continue

            reasons = []
            if request.technician_id and str(candidate.technician_id) == str(
                request.technician_id
            ):
                reasons.append("technician is already booked")
            if request.bay and candidate.bay == request.bay:
                reasons.append(f"bay '{request.bay}' is already occupied")
            if request.vehicle_id and str(candidate.vehicle_id) == str(request.vehicle_id):
                reasons.append("vehicle is already scheduled")

            if reasons:
                conflicts.append(
                    ConflictDetail(
                        appointment_id=candidate.id,
                        reason="; ".join(reasons),
                        scheduled_start=candidate.scheduled_start,
                        scheduled_end=candidate.scheduled_end,
                    )
                )

        return conflicts

    async def check_conflicts(self, request: ConflictCheckRequest) -> ConflictCheckResponse:
        """Conflict check as an API response."""
        conflicts = await self.find_conflicts(request)
        return ConflictCheckResponse(has_conflict=bool(conflicts), conflicts=conflicts)

    async def _assert_no_conflicts(
        self,
        *,
        scheduled_start: datetime,
        duration_minutes: int,
        technician_id: UUID | str | None,
        bay: str | None,
        vehicle_id: UUID | str | None,
        exclude_appointment_id: UUID | str | None = None,
    ) -> None:
        """Raise ConflictError if the slot is already taken."""
        conflicts = await self.find_conflicts(
            ConflictCheckRequest(
                scheduled_start=scheduled_start,
                duration_minutes=duration_minutes,
                technician_id=technician_id,
                bay=bay,
                vehicle_id=vehicle_id,
                exclude_appointment_id=exclude_appointment_id,
            )
        )
        if conflicts:
            detail = "; ".join(
                f"{c.reason} ({c.scheduled_start:%Y-%m-%d %H:%M}"
                f"-{c.scheduled_end:%H:%M})"
                for c in conflicts
            )
            raise ConflictError(f"Scheduling conflict: {detail}")

    # --- validation ---------------------------------------------------------

    async def _assert_related_records_exist(
        self,
        *,
        customer_id: UUID | str,
        vehicle_id: UUID | str,
        service_request_id: UUID | str | None = None,
        advisor_id: UUID | str | None = None,
        technician_id: UUID | str | None = None,
    ) -> None:
        """Verify FK targets exist and the vehicle belongs to the customer."""
        from app.auth.models import User
        from app.customers.models import Customer
        from app.service_requests.models import ServiceRequest
        from app.vehicles.models import Vehicle

        customer = (
            await self.db.execute(select(Customer).where(Customer.id == str(customer_id)))
        ).scalar_one_or_none()
        if not customer:
            raise ConflictError(f"Customer with id {customer_id} not found")

        vehicle = (
            await self.db.execute(select(Vehicle).where(Vehicle.id == str(vehicle_id)))
        ).scalar_one_or_none()
        if not vehicle:
            raise ConflictError(f"Vehicle with id {vehicle_id} not found")

        if str(vehicle.customer_id) != str(customer_id):
            raise BusinessRuleError(
                f"Vehicle {vehicle_id} does not belong to customer {customer_id}"
            )

        if service_request_id:
            service_request = (
                await self.db.execute(
                    select(ServiceRequest).where(ServiceRequest.id == str(service_request_id))
                )
            ).scalar_one_or_none()
            if not service_request:
                raise ConflictError(f"Service request {service_request_id} not found")

        for user_id, label in ((advisor_id, "Advisor"), (technician_id, "Technician")):
            if not user_id:
                continue
            user = (
                await self.db.execute(select(User).where(User.id == str(user_id)))
            ).scalar_one_or_none()
            if not user:
                raise ConflictError(f"{label} with id {user_id} not found")

    # --- writes -------------------------------------------------------------

    async def create_appointment(self, data: AppointmentCreate) -> Appointment:
        """Book an appointment, rejecting slots that clash with existing work."""
        await self._assert_related_records_exist(
            customer_id=data.customer_id,
            vehicle_id=data.vehicle_id,
            service_request_id=data.service_request_id,
            advisor_id=data.advisor_id,
            technician_id=data.technician_id,
        )
        await self._assert_no_conflicts(
            scheduled_start=data.scheduled_start,
            duration_minutes=data.duration_minutes,
            technician_id=data.technician_id,
            bay=data.bay,
            vehicle_id=data.vehicle_id,
        )

        appointment = Appointment(
            customer_id=str(data.customer_id),
            vehicle_id=str(data.vehicle_id),
            service_request_id=(
                str(data.service_request_id) if data.service_request_id else None
            ),
            service_type=data.service_type,
            scheduled_start=data.scheduled_start,
            duration_minutes=data.duration_minutes,
            advisor_id=str(data.advisor_id) if data.advisor_id else None,
            technician_id=str(data.technician_id) if data.technician_id else None,
            bay=data.bay,
            customer_concern=data.customer_concern,
            notes=data.notes,
        )
        self.db.add(appointment)
        await self.db.commit()
        await self.db.refresh(appointment)
        logger.info("Appointment created: %s", appointment.id)
        return appointment

    async def create_from_service_request(
        self, service_request_id: UUID | str, data: AppointmentFromServiceRequest
    ) -> Appointment:
        """Turn an approved service request into a booked appointment.

        The service request's customer, vehicle and description seed the
        appointment, and the request moves to CONVERTED. Both writes share one
        transaction, so a scheduling conflict leaves the request untouched
        rather than marking it converted with nothing on the calendar.
        """
        from app.service_requests.models import ServiceRequest, ServiceRequestStatus

        service_request = (
            await self.db.execute(
                select(ServiceRequest).where(ServiceRequest.id == str(service_request_id))
            )
        ).scalar_one_or_none()
        if not service_request:
            raise NotFoundError(f"Service request {service_request_id} not found")

        if service_request.status == ServiceRequestStatus.CONVERTED.value:
            raise BusinessRuleError("Service request has already been converted")
        if service_request.status != ServiceRequestStatus.APPROVED.value:
            raise BusinessRuleError(
                f"Service request must be APPROVED before scheduling "
                f"(currently {service_request.status})"
            )

        vehicle_id = data.vehicle_id or service_request.vehicle_id
        if not vehicle_id:
            raise BusinessRuleError(
                "Service request has no vehicle; supply vehicle_id when scheduling"
            )

        await self._assert_related_records_exist(
            customer_id=service_request.customer_id,
            vehicle_id=vehicle_id,
            advisor_id=data.advisor_id,
            technician_id=data.technician_id,
        )
        await self._assert_no_conflicts(
            scheduled_start=data.scheduled_start,
            duration_minutes=data.duration_minutes,
            technician_id=data.technician_id,
            bay=data.bay,
            vehicle_id=vehicle_id,
        )

        appointment = Appointment(
            customer_id=str(service_request.customer_id),
            vehicle_id=str(vehicle_id),
            service_request_id=str(service_request.id),
            service_type=data.service_type,
            scheduled_start=data.scheduled_start,
            duration_minutes=data.duration_minutes,
            advisor_id=str(data.advisor_id) if data.advisor_id else None,
            technician_id=str(data.technician_id) if data.technician_id else None,
            bay=data.bay,
            customer_concern=data.customer_concern or service_request.description,
            notes=data.notes,
            status=AppointmentStatus.CONFIRMED.value,
        )
        self.db.add(appointment)
        service_request.status = ServiceRequestStatus.CONVERTED.value

        await self.db.commit()
        await self.db.refresh(appointment)
        logger.info(
            "Service request %s converted to appointment %s",
            service_request.id,
            appointment.id,
        )
        return appointment

    async def update_appointment(
        self, appointment_id: UUID | str, data: AppointmentUpdate
    ) -> Appointment:
        """Update an appointment, re-checking conflicts if the slot moves."""
        appointment = await self.get_by_id(appointment_id)
        changes = data.model_dump(exclude_unset=True)

        reschedule_fields = {"scheduled_start", "duration_minutes", "technician_id", "bay"}
        if reschedule_fields & changes.keys() and appointment.status in _UNRESCHEDULABLE:
                raise BusinessRuleError(
                    f"Appointment is {appointment.status} and can no longer be rescheduled"
                )

        if "technician_id" in changes or "advisor_id" in changes:
            await self._assert_related_records_exist(
                customer_id=appointment.customer_id,
                vehicle_id=appointment.vehicle_id,
                advisor_id=changes.get("advisor_id", appointment.advisor_id),
                technician_id=changes.get("technician_id", appointment.technician_id),
            )

        new_start = changes.get("scheduled_start", appointment.scheduled_start)
        new_duration = changes.get("duration_minutes", appointment.duration_minutes)
        new_technician = changes.get("technician_id", appointment.technician_id)
        new_bay = changes.get("bay", appointment.bay)

        if reschedule_fields & changes.keys():
            await self._assert_no_conflicts(
                scheduled_start=new_start,
                duration_minutes=new_duration,
                technician_id=new_technician,
                bay=new_bay,
                vehicle_id=appointment.vehicle_id,
                exclude_appointment_id=appointment.id,
            )

        for field, value in changes.items():
            setattr(
                appointment,
                field,
                str(value) if field.endswith("_id") and value is not None else value,
            )

        await self.db.commit()
        await self.db.refresh(appointment)
        return appointment

    async def update_status(
        self, appointment_id: UUID | str, data: AppointmentStatusUpdate
    ) -> Appointment:
        """Transition an appointment to a new status."""
        appointment = await self.get_by_id(appointment_id)
        _validate_status_transition(appointment.status, data.status)

        if (
            data.status == AppointmentStatus.CANCELLED.value
            and data.cancellation_reason
        ):
            appointment.cancellation_reason = data.cancellation_reason

        appointment.status = data.status
        await self.db.commit()
        await self.db.refresh(appointment)
        logger.info("Appointment %s -> %s", appointment.id, data.status)
        return appointment

    async def delete_appointment(self, appointment_id: UUID | str) -> None:
        """Delete an appointment.

        Cancelling is almost always the right action; deletion exists for
        cleaning up records created in error.
        """
        appointment = await self.get_by_id(appointment_id)
        await self.db.delete(appointment)
        await self.db.commit()
        logger.info("Appointment deleted: %s", appointment_id)

    # --- calendar -----------------------------------------------------------

    @staticmethod
    def resolve_range(view: str, anchor: date) -> tuple[datetime, datetime]:
        """Resolve a calendar view and anchor date into [start, end) bounds."""
        view = view.lower()
        if view not in CALENDAR_VIEWS:
            raise BusinessRuleError(
                f"Invalid calendar view '{view}'. Allowed: {', '.join(CALENDAR_VIEWS)}"
            )

        if view == "day":
            start_date, end_date = anchor, anchor + timedelta(days=1)
        elif view == "week":
            # Weeks run Monday to Sunday.
            start_date = anchor - timedelta(days=anchor.weekday())
            end_date = start_date + timedelta(days=7)
        else:
            start_date = anchor.replace(day=1)
            if start_date.month == 12:
                end_date = start_date.replace(year=start_date.year + 1, month=1)
            else:
                end_date = start_date.replace(month=start_date.month + 1)

        start = datetime.combine(start_date, time.min, tzinfo=UTC)
        end = datetime.combine(end_date, time.min, tzinfo=UTC)
        return start, end

    async def get_calendar(
        self,
        *,
        view: str,
        anchor: date,
        technician_id: UUID | str | None = None,
        advisor_id: UUID | str | None = None,
        bay: str | None = None,
        include_cancelled: bool = False,
    ) -> CalendarResponse:
        """Build a day / week / month calendar, grouped by date."""
        start, end = self.resolve_range(view, anchor)

        stmt = (
            select(Appointment)
            .where(Appointment.scheduled_start >= start)
            .where(Appointment.scheduled_start < end)
            .order_by(Appointment.scheduled_start.asc())
        )
        if technician_id:
            stmt = stmt.where(Appointment.technician_id == str(technician_id))
        if advisor_id:
            stmt = stmt.where(Appointment.advisor_id == str(advisor_id))
        if bay:
            stmt = stmt.where(Appointment.bay == bay)
        if not include_cancelled:
            stmt = stmt.where(
                Appointment.status.not_in(
                    (
                        AppointmentStatus.CANCELLED.value,
                        AppointmentStatus.NO_SHOW.value,
                    )
                )
            )

        appointments = list((await self.db.execute(stmt)).scalars().all())

        grouped: dict[date, list[Appointment]] = {}
        for appointment in appointments:
            day = _as_aware(appointment.scheduled_start).date()
            grouped.setdefault(day, []).append(appointment)

        # Every day in the range appears, including empty ones, so the client
        # can render a calendar grid without filling gaps itself.
        days: list[CalendarDay] = []
        cursor = start.date()
        while cursor < end.date():
            days.append(
                CalendarDay(
                    date=cursor,
                    appointments=[
                        AppointmentRead.model_validate(a) for a in grouped.get(cursor, [])
                    ],
                )
            )
            cursor += timedelta(days=1)

        return CalendarResponse(
            view=view.lower(),
            range_start=start,
            range_end=end,
            total=len(appointments),
            days=days,
        )
