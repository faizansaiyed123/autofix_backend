"""Vehicle check-in data models.

Check-ins represent the initial intake of a vehicle when a customer
brings it in for service. Captures vehicle condition, odometer reading,
and check-in metadata.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import TimestampedBase


class CheckInType(str, enum.Enum):
    DRIVE_IN = "DRIVE_IN"
    WALK_IN = "WALK_IN"
    APPOINTMENT = "APPOINTMENT"


class CheckInStatus(str, enum.Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class CheckIn(TimestampedBase):
    """Check-in entity for vehicle service intake."""

    __tablename__ = "check_ins"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vehicle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    odometer: Mapped[int] = mapped_column(nullable=False)
    checkin_type: Mapped[str] = mapped_column(String(20), default=CheckInType.DRIVE_IN.value, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=CheckInStatus.PENDING.value, nullable=False)
    service_advisor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_completion: Mapped[str | None] = mapped_column(nullable=True)
    tire_condition: Mapped[str | None] = mapped_column(String(50), nullable=True)
    fluid_levels: Mapped[str | None] = mapped_column(String(50), nullable=True)
    lights_status: Mapped[str | None] = mapped_column(String(50), nullable=True)

    def __repr__(self) -> str:
        return f"<CheckIn({self.id})>"
