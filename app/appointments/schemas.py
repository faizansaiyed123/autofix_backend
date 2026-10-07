"""Pydantic schemas for appointment scheduling."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from pydantic import Field, field_validator, model_validator

from app.appointments.models import AppointmentStatus, ServiceType
from app.common.schemas import BaseSchema

# A slot shorter than this is almost certainly a data-entry mistake, and a
# slot longer than a day belongs in a repair order, not the calendar.
MIN_DURATION_MINUTES = 5
MAX_DURATION_MINUTES = 1440


def _validate_enum(value: str | None, enum_cls, field_name: str) -> str | None:
    """Coerce and validate an incoming string against an enum."""
    if value is None:
        return None
    try:
        return enum_cls(value.upper()).value
    except ValueError:
        allowed = ", ".join(m.value for m in enum_cls)
        raise ValueError(f"Invalid {field_name} '{value}'. Allowed: {allowed}")


def _as_aware(value: datetime) -> datetime:
    """Treat a naive datetime as UTC so comparisons never raise."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class AppointmentBase(BaseSchema):
    customer_id: uuid.UUID
    vehicle_id: uuid.UUID
    service_request_id: uuid.UUID | None = None
    service_type: str = ServiceType.OTHER.value
    scheduled_start: datetime
    duration_minutes: int = Field(
        60, ge=MIN_DURATION_MINUTES, le=MAX_DURATION_MINUTES
    )
    advisor_id: uuid.UUID | None = None
    technician_id: uuid.UUID | None = None
    bay: str | None = Field(None, max_length=30)
    customer_concern: str | None = Field(None, max_length=2000)
    notes: str | None = Field(None, max_length=2000)

    @field_validator("service_type")
    @classmethod
    def _check_service_type(cls, v: str) -> str:
        return _validate_enum(v, ServiceType, "service type")

    @field_validator("scheduled_start")
    @classmethod
    def _normalise_start(cls, v: datetime) -> datetime:
        return _as_aware(v)


class AppointmentCreate(AppointmentBase):
    """Payload for booking an appointment."""


class AppointmentFromServiceRequest(BaseSchema):
    """Scheduling details for converting a service request into a booking.

    Customer, vehicle and concern are inherited from the service request;
    vehicle_id is only needed when the request did not name one.
    """

    scheduled_start: datetime
    duration_minutes: int = Field(
        60, ge=MIN_DURATION_MINUTES, le=MAX_DURATION_MINUTES
    )
    vehicle_id: uuid.UUID | None = None
    service_type: str = ServiceType.OTHER.value
    advisor_id: uuid.UUID | None = None
    technician_id: uuid.UUID | None = None
    bay: str | None = Field(None, max_length=30)
    customer_concern: str | None = Field(None, max_length=2000)
    notes: str | None = Field(None, max_length=2000)

    @field_validator("service_type")
    @classmethod
    def _check_service_type(cls, v: str) -> str:
        return _validate_enum(v, ServiceType, "service type")

    @field_validator("scheduled_start")
    @classmethod
    def _normalise_start(cls, v: datetime) -> datetime:
        return _as_aware(v)


class AppointmentUpdate(BaseSchema):
    """Partial update. Rescheduling re-runs conflict detection."""

    service_type: str | None = None
    scheduled_start: datetime | None = None
    duration_minutes: int | None = Field(
        None, ge=MIN_DURATION_MINUTES, le=MAX_DURATION_MINUTES
    )
    advisor_id: uuid.UUID | None = None
    technician_id: uuid.UUID | None = None
    bay: str | None = Field(None, max_length=30)
    customer_concern: str | None = Field(None, max_length=2000)
    notes: str | None = Field(None, max_length=2000)

    @field_validator("service_type")
    @classmethod
    def _check_service_type(cls, v: str | None) -> str | None:
        return _validate_enum(v, ServiceType, "service type")

    @field_validator("scheduled_start")
    @classmethod
    def _normalise_start(cls, v: datetime | None) -> datetime | None:
        return _as_aware(v) if v else None


class AppointmentStatusUpdate(BaseSchema):
    """Status transition, with an optional reason for cancellations."""

    status: str
    cancellation_reason: str | None = Field(None, max_length=1000)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        return _validate_enum(v, AppointmentStatus, "appointment status")


class AppointmentRead(AppointmentBase):
    id: uuid.UUID
    checkin_id: uuid.UUID | None = None
    status: str
    scheduled_end: datetime
    cancellation_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class CalendarQuery(BaseSchema):
    """Resolved bounds for a calendar view."""

    view: str
    start: datetime
    end: datetime


class CalendarDay(BaseSchema):
    """One day's appointments within a calendar view."""

    date: date
    appointments: list[AppointmentRead]


class CalendarResponse(BaseSchema):
    """Day / week / month calendar payload."""

    view: str
    range_start: datetime
    range_end: datetime
    total: int
    days: list[CalendarDay]


class ConflictCheckRequest(BaseSchema):
    """Ask whether a prospective slot would collide with existing bookings."""

    scheduled_start: datetime
    duration_minutes: int = Field(
        60, ge=MIN_DURATION_MINUTES, le=MAX_DURATION_MINUTES
    )
    technician_id: uuid.UUID | None = None
    bay: str | None = Field(None, max_length=30)
    vehicle_id: uuid.UUID | None = None
    exclude_appointment_id: uuid.UUID | None = None

    @field_validator("scheduled_start")
    @classmethod
    def _normalise_start(cls, v: datetime) -> datetime:
        return _as_aware(v)


class ConflictDetail(BaseSchema):
    """A single detected clash."""

    appointment_id: uuid.UUID
    reason: str
    scheduled_start: datetime
    scheduled_end: datetime


class ConflictCheckResponse(BaseSchema):
    has_conflict: bool
    conflicts: list[ConflictDetail]


class AvailabilityQuery(BaseSchema):
    """Bounds for a free-slot search."""

    day: date
    duration_minutes: int = Field(
        60, ge=MIN_DURATION_MINUTES, le=MAX_DURATION_MINUTES
    )
    technician_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _check(self) -> AvailabilityQuery:
        return self
