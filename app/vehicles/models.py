"""Vehicle data models.

Vehicles belong to customers and track vehicle-specific information
including VIN, license plate, mileage history, and service records.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TimestampedBase


class VehicleStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    IN_SHOP = "IN_SHOP"
    ARCHIVED = "ARCHIVED"


class FuelType(str, enum.Enum):
    GASOLINE = "GASOLINE"
    DIESEL = "DIESEL"
    ELECTRIC = "ELECTRIC"
    HYBRID = "HYBRID"
    OTHER = "OTHER"


class Vehicle(TimestampedBase):
    """Vehicle entity - owned by a customer."""

    __tablename__ = "vehicles"

    vin: Mapped[str | None] = mapped_column(String(17), unique=True, index=True, nullable=True)
    license_plate: Mapped[str | None] = mapped_column(String(20), index=True, nullable=True)
    make: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    year: Mapped[int] = mapped_column(nullable=True)
    trim: Mapped[str | None] = mapped_column(String(100), nullable=True)
    engine: Mapped[str | None] = mapped_column(String(100), nullable=True)
    transmission: Mapped[str | None] = mapped_column(String(50), nullable=True)
    mileage: Mapped[int | None] = mapped_column(nullable=True)
    color: Mapped[str | None] = mapped_column(String(30), nullable=True)
    fuel_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    purchase_date: Mapped[str | None] = mapped_column(nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=VehicleStatus.ACTIVE.value, nullable=False)

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )

    mileage_records: Mapped[list[VehicleMileageRecord]] = relationship(
        "VehicleMileageRecord", back_populates="vehicle",
        cascade="all, delete-orphan", lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Vehicle({self.year} {self.make} {self.model})>"


class MileageSource(str, enum.Enum):
    MANUAL = "MANUAL"
    REPAIR_ORDER = "REPAIR_ORDER"
    CHECK_IN = "CHECK_IN"
    INSPECTION = "INSPECTION"


class VehicleMileageRecord(TimestampedBase):
    """Mileage history record for a vehicle."""

    __tablename__ = "vehicle_mileage_records"

    vehicle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mileage: Mapped[int] = mapped_column(nullable=False)
    source: Mapped[str] = mapped_column(String(20), default=MileageSource.MANUAL.value, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    vehicle: Mapped[Vehicle] = relationship("Vehicle", back_populates="mileage_records")

    def __repr__(self) -> str:
        return f"<VehicleMileageRecord(mileage={self.mileage})>"
