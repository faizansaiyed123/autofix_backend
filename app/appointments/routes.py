"""API routes for appointment scheduling.

Endpoints:
- GET    /                    List appointments (paginated, filterable)
- POST   /                    Book an appointment
- POST   /from-service-request/{id}  Convert a service request into a booking
- GET    /calendar            Day / week / month calendar view
- POST   /check-conflicts     Test a prospective slot without booking it
- GET    /{id}                Get an appointment
- PATCH  /{id}                Update / reschedule an appointment
- PATCH  /{id}/status         Transition appointment status
- DELETE /{id}                Delete an appointment
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.appointments.schemas import (
    AppointmentCreate,
    AppointmentFromServiceRequest,
    AppointmentRead,
    AppointmentStatusUpdate,
    AppointmentUpdate,
    CalendarResponse,
    ConflictCheckRequest,
    ConflictCheckResponse,
)
from app.appointments.services import AppointmentService
from app.auth.dependencies import require_permission
from app.auth.models import User
from app.auth.permissions import PermissionEnum
from app.common.schemas import PaginatedResponse
from app.core.database import AsyncSession, get_session

router = APIRouter()


@router.get("/", response_model=PaginatedResponse[AppointmentRead])
async def list_appointments(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    customer_id: UUID | None = Query(None),
    vehicle_id: UUID | None = Query(None),
    technician_id: UUID | None = Query(None),
    advisor_id: UUID | None = Query(None),
    bay: str | None = Query(None),
    start_from: datetime | None = Query(None),
    start_to: datetime | None = Query(None),
    current_user: User = Depends(require_permission(PermissionEnum.APPOINTMENTS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """List appointments with pagination and filtering."""
    service = AppointmentService(session)
    appointments, total = await service.list_appointments(
        page=page,
        size=size,
        status=status,
        customer_id=customer_id,
        vehicle_id=vehicle_id,
        technician_id=technician_id,
        advisor_id=advisor_id,
        bay=bay,
        start_from=start_from,
        start_to=start_to,
    )
    return PaginatedResponse[AppointmentRead].create(
        items=[AppointmentRead.model_validate(a) for a in appointments],
        page=page,
        size=size,
        total=total,
    )


@router.post("/", response_model=AppointmentRead, status_code=status.HTTP_201_CREATED)
async def create_appointment(
    appointment_data: AppointmentCreate,
    current_user: User = Depends(require_permission(PermissionEnum.APPOINTMENTS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Book a new appointment. Rejects slots that clash with existing work."""
    service = AppointmentService(session)
    appointment = await service.create_appointment(appointment_data)
    return AppointmentRead.model_validate(appointment)


@router.post(
    "/from-service-request/{service_request_id}",
    response_model=AppointmentRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_from_service_request(
    service_request_id: UUID,
    scheduling: AppointmentFromServiceRequest,
    current_user: User = Depends(require_permission(PermissionEnum.APPOINTMENTS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Convert an approved service request into a confirmed appointment."""
    service = AppointmentService(session)
    appointment = await service.create_from_service_request(service_request_id, scheduling)
    return AppointmentRead.model_validate(appointment)


@router.get("/calendar", response_model=CalendarResponse)
async def get_calendar(
    view: str = Query("week", description="day, week or month"),
    anchor: date | None = Query(
        None, description="Any date inside the desired range. Defaults to today."
    ),
    technician_id: UUID | None = Query(None),
    advisor_id: UUID | None = Query(None),
    bay: str | None = Query(None),
    include_cancelled: bool = Query(False),
    current_user: User = Depends(require_permission(PermissionEnum.APPOINTMENTS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Day, week or month calendar, grouped by date."""
    service = AppointmentService(session)
    return await service.get_calendar(
        view=view,
        # "Today" here means the shop's local day, not UTC, so an evening request
        # does not land on tomorrow's calendar.
        anchor=anchor or date.today(),  # noqa: DTZ011
        technician_id=technician_id,
        advisor_id=advisor_id,
        bay=bay,
        include_cancelled=include_cancelled,
    )


@router.post("/check-conflicts", response_model=ConflictCheckResponse)
async def check_conflicts(
    request: ConflictCheckRequest,
    current_user: User = Depends(require_permission(PermissionEnum.APPOINTMENTS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Test whether a prospective slot would clash, without booking it."""
    service = AppointmentService(session)
    return await service.check_conflicts(request)


@router.get("/{appointment_id}", response_model=AppointmentRead)
async def get_appointment(
    appointment_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.APPOINTMENTS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get an appointment by ID."""
    service = AppointmentService(session)
    return AppointmentRead.model_validate(await service.get_by_id(appointment_id))


@router.patch("/{appointment_id}", response_model=AppointmentRead)
async def update_appointment(
    appointment_id: UUID,
    appointment_data: AppointmentUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.APPOINTMENTS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Update or reschedule an appointment."""
    service = AppointmentService(session)
    appointment = await service.update_appointment(appointment_id, appointment_data)
    return AppointmentRead.model_validate(appointment)


@router.patch("/{appointment_id}/status", response_model=AppointmentRead)
async def update_appointment_status(
    appointment_id: UUID,
    status_data: AppointmentStatusUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.APPOINTMENTS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Transition an appointment to a new status."""
    service = AppointmentService(session)
    appointment = await service.update_status(appointment_id, status_data)
    return AppointmentRead.model_validate(appointment)


@router.delete("/{appointment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_appointment(
    appointment_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.APPOINTMENTS_MANAGE)),
    session: AsyncSession = Depends(get_session),
):
    """Delete an appointment. Prefer cancelling where there is a real booking."""
    service = AppointmentService(session)
    await service.delete_appointment(appointment_id)
