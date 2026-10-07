"""Service request data models.

Service requests are initial intake forms when customers request service.
They can be converted to repair orders once approved.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import TimestampedBase


class ServiceRequestStatus(str, enum.Enum):
    NEW = "NEW"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CONVERTED = "CONVERTED"


class ServiceRequestPriority(str, enum.Enum):
    LOW = "LOW"
    STANDARD = "STANDARD"
    HIGH = "HIGH"
    EMERGENCY = "EMERGENCY"


class ServiceRequest(TimestampedBase):
    """Service request entity - initial customer service inquiry."""

    __tablename__ = "service_requests"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vehicles.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(String(20), default=ServiceRequestPriority.STANDARD.value, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=ServiceRequestStatus.NEW.value, nullable=False)
    service_advisor_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<ServiceRequest({self.title})>"
