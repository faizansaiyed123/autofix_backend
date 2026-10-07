"""Appointment data models.

An appointment is a scheduled slot in the shop's calendar: a customer brings
a vehicle in at a given time, for an expected duration, usually handled by a
named advisor and optionally a named technician.

Scheduling is stored as a start timestamp plus a duration in minutes rather
than as separate date/time/end columns. One instant plus a length is
unambiguous across time zones and makes overlap queries a simple comparison,
whereas a split date + time + end-time triple has to be reassembled (and kept
consistent) on every read.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timedelta

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.core.database import TimestampedBase


class AppointmentStatus(str, enum.Enum):
    """Lifecycle of a scheduled appointment."""

    REQUESTED = "REQUESTED"
    CONFIRMED = "CONFIRMED"
    CHECKED_IN = "CHECKED_IN"
    IN_SERVICE = "IN_SERVICE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    NO_SHOW = "NO_SHOW"


class ServiceType(str, enum.Enum):
    """Broad category of work the appointment is booked for."""

    OIL_CHANGE = "OIL_CHANGE"
    BRAKE_SERVICE = "BRAKE_SERVICE"
    TIRE_SERVICE = "TIRE_SERVICE"
    DIAGNOSTIC = "DIAGNOSTIC"
    SCHEDULED_MAINTENANCE = "SCHEDULED_MAINTENANCE"
    INSPECTION = "INSPECTION"
    REPAIR = "REPAIR"
    OTHER = "OTHER"


# Statuses where the slot is no longer actually occupied, so it should not
# block another booking.
RELEASED_STATUSES: frozenset[str] = frozenset(
    {
        AppointmentStatus.CANCELLED.value,
        AppointmentStatus.NO_SHOW.value,
        AppointmentStatus.COMPLETED.value,
    }
)

# Statuses that mean the vehicle is physically in the shop.
ACTIVE_IN_SHOP_STATUSES: frozenset[str] = frozenset(
    {
        AppointmentStatus.CHECKED_IN.value,
        AppointmentStatus.IN_SERVICE.value,
    }
)


class Appointment(TimestampedBase):
    """A scheduled service appointment."""

    __tablename__ = "appointments"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vehicle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    service_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_requests.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    checkin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("check_ins.id", ondelete="SET NULL"), nullable=True
    )

    service_type: Mapped[str] = mapped_column(
        String(30), default=ServiceType.OTHER.value, nullable=False
    )
    scheduled_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    duration_minutes: Mapped[int] = mapped_column(nullable=False, default=60)

    advisor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    technician_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    bay: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)

    customer_concern: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default=AppointmentStatus.REQUESTED.value, nullable=False, index=True
    )
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "duration_minutes > 0 AND duration_minutes <= 1440",
            name="ck_appointments_duration",
        ),
        # Conflict detection queries filter on status and then scan a time
        # window, so the composite index matches the access pattern.
        Index("ix_appointments_status_start", "status", "scheduled_start"),
    )

    @property
    def scheduled_end(self) -> datetime:
        """Instant the appointment is expected to finish."""
        return self.scheduled_start + timedelta(minutes=self.duration_minutes)

    @property
    def occupies_slot(self) -> bool:
        """Whether this appointment still reserves its time slot."""
        return self.status not in RELEASED_STATUSES

    def overlaps(self, start: datetime, end: datetime) -> bool:
        """Whether this appointment's window intersects [start, end).

        Touching endpoints do not count: an appointment ending at 10:00 does
        not conflict with one starting at 10:00.
        """
        return self.scheduled_start < end and start < self.scheduled_end

    def __repr__(self) -> str:
        return f"<Appointment({self.scheduled_start:%Y-%m-%d %H:%M} {self.status})>"
