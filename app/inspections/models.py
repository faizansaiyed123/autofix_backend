"""Digital vehicle inspection data models.

Inspections capture the condition of a vehicle across multiple
categories (brakes, engine, fluids, etc.) with photo documentation.

Photo ownership is one-directional: an ``InspectionPhoto`` points at the
``InspectionItem`` it documents. An earlier revision also carried an
``inspection_items.photo_id`` column pointing the other way, which created a
circular foreign key (neither table could be created first) and limited an
item to a single photo. Photos now hang off the item only.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TimestampedBase


class InspectionStatus(str, enum.Enum):
    """Lifecycle of an inspection session."""

    DRAFT = "DRAFT"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class InspectionItemStatus(str, enum.Enum):
    """Condition of a single inspected item.

    These map onto the customer-facing traffic-light report:
        GOOD        -> GREEN   (no action needed)
        ATTENTION   -> YELLOW  (monitor / will need work soon)
        RECOMMENDED -> YELLOW  (work advised now, not safety-critical)
        URGENT      -> RED     (safety-critical, address immediately)
        NOT_CHECKED -> grey    (not inspected / not applicable)
    """

    GOOD = "GOOD"
    ATTENTION = "ATTENTION"
    RECOMMENDED = "RECOMMENDED"
    URGENT = "URGENT"
    NOT_CHECKED = "NOT_CHECKED"


class InspectionRecommendation(str, enum.Enum):
    """Suggested action for an inspected item."""

    PASS = "PASS"
    MONITOR = "MONITOR"
    ADJUST = "ADJUST"
    CLEAN = "CLEAN"
    LUBE = "LUBE"
    REPAIR = "REPAIR"
    REPLACE = "REPLACE"
    INSPECT_FURTHER = "INSPECT_FURTHER"


# Severity ordering, used to derive an inspection's overall condition.
ITEM_STATUS_SEVERITY: dict[str, int] = {
    InspectionItemStatus.NOT_CHECKED.value: 0,
    InspectionItemStatus.GOOD.value: 1,
    InspectionItemStatus.ATTENTION.value: 2,
    InspectionItemStatus.RECOMMENDED.value: 3,
    InspectionItemStatus.URGENT.value: 4,
}

# Traffic-light bucket for the customer-facing report.
ITEM_STATUS_SEVERITY_COLOR: dict[str, str] = {
    InspectionItemStatus.GOOD.value: "GREEN",
    InspectionItemStatus.ATTENTION.value: "YELLOW",
    InspectionItemStatus.RECOMMENDED.value: "YELLOW",
    InspectionItemStatus.URGENT.value: "RED",
    InspectionItemStatus.NOT_CHECKED.value: "GREY",
}


class Inspection(TimestampedBase):
    """A digital vehicle inspection session."""

    __tablename__ = "inspections"

    vehicle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    checkin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("check_ins.id", ondelete="SET NULL"), nullable=True
    )
    technician_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), default=InspectionStatus.DRAFT.value, nullable=False, index=True
    )
    mileage: Mapped[int | None] = mapped_column(nullable=True)
    overall_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    items: Mapped[list[InspectionItem]] = relationship(
        "InspectionItem",
        back_populates="inspection",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="InspectionItem.created_at",
    )

    @property
    def overall_condition(self) -> str:
        """Worst item status across the inspection, as a traffic-light colour."""
        if not self.items:
            return "GREY"
        worst = max(
            self.items,
            key=lambda i: ITEM_STATUS_SEVERITY.get(i.status, 0),
        )
        return ITEM_STATUS_SEVERITY_COLOR.get(worst.status, "GREY")

    def __repr__(self) -> str:
        return f"<Inspection({self.id} {self.status})>"


class InspectionItem(TimestampedBase):
    """A single inspected item within an inspection."""

    __tablename__ = "inspection_items"

    inspection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inspections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    item_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=InspectionItemStatus.GOOD.value, nullable=False
    )
    measurement: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommendation: Mapped[str | None] = mapped_column(String(20), nullable=True)

    inspection: Mapped[Inspection] = relationship("Inspection", back_populates="items")
    photos: Mapped[list[InspectionPhoto]] = relationship(
        "InspectionPhoto",
        back_populates="item",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="InspectionPhoto.created_at",
    )

    @property
    def severity_color(self) -> str:
        """Traffic-light bucket for this item."""
        return ITEM_STATUS_SEVERITY_COLOR.get(self.status, "GREY")

    @property
    def photo_url(self) -> str | None:
        """URL of the first photo, for compact list views."""
        return self.photos[0].photo_url if self.photos else None

    def __repr__(self) -> str:
        return f"<InspectionItem({self.category}: {self.item_name} = {self.status})>"


class InspectionPhoto(TimestampedBase):
    """A photo documenting an inspection item."""

    __tablename__ = "inspection_photos"

    inspection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inspections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    inspection_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inspection_items.id", ondelete="CASCADE"), nullable=True, index=True
    )
    photo_url: Mapped[str] = mapped_column(String(500), nullable=False)
    caption: Mapped[str | None] = mapped_column(String(200), nullable=True)

    item: Mapped[InspectionItem | None] = relationship("InspectionItem", back_populates="photos")

    def __repr__(self) -> str:
        return f"<InspectionPhoto({self.id})>"
