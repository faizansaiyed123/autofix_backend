"""Estimate data models.

An estimate is the priced proposal sent to a customer before repair work
starts. It is built from line items (labor, parts, services, fees and
discounts), carries the money totals, and records the customer's decision
item by item so partial approvals are first-class rather than an
afterthought.

Money is stored as ``NUMERIC(12, 2)`` and read back as ``float``
(``asdecimal=False``): the API contract is JSON numbers, and doing the
arithmetic in ``Decimal`` only to serialise it away would add rounding
rules the callers cannot see. Every derived amount is rounded to cents at
the point it is written to the column.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TimestampedBase

# Statuses that mean the estimate is no longer open for customer decisions.
CLOSED_ESTIMATE_STATUSES = frozenset(
    {
        "APPROVED",
        "PARTIALLY_APPROVED",
        "DECLINED",
        "EXPIRED",
    }
)

# Estimate statuses that await a customer decision.
AWAITING_DECISION_STATUSES = frozenset({"SENT"})

CENTS = 2


def round_money(value: float) -> float:
    """Round a monetary amount to cents.

    Banker's rounding at the half-cent is avoided by nudging the value off
    the exact midpoint before rounding, so ``0.125`` becomes ``0.13`` rather
    than an arbitrary parity-dependent result.
    """
    return round(value + 1e-9, CENTS)


class EstimateStatus(str, enum.Enum):
    """Lifecycle of an estimate."""

    DRAFT = "DRAFT"
    SENT = "SENT"
    PARTIALLY_APPROVED = "PARTIALLY_APPROVED"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class EstimateItemType(str, enum.Enum):
    """Kind of charge an estimate line represents."""

    LABOR = "LABOR"
    PART = "PART"
    SERVICE = "SERVICE"
    FEE = "FEE"
    DISCOUNT = "DISCOUNT"


class EstimateItemStatus(str, enum.Enum):
    """Customer decision on a single estimate line."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"


# Item types that add to the estimate rather than reducing it.
CHARGE_ITEM_TYPES = frozenset(
    {
        EstimateItemType.LABOR.value,
        EstimateItemType.PART.value,
        EstimateItemType.SERVICE.value,
        EstimateItemType.FEE.value,
    }
)

# Estimate statuses that await a customer decision.
AWAITING_DECISION_STATUSES = frozenset({EstimateStatus.SENT.value})

# A partially approved estimate is still awaiting decisions on its open lines.
PARTIAL_DECISION_STATUSES = frozenset({EstimateStatus.PARTIALLY_APPROVED.value})

ESTIMATE_STATUS_TRANSITIONS: dict[str, list[str]] = {
    EstimateStatus.DRAFT.value: [
        EstimateStatus.SENT.value,
        EstimateStatus.CANCELLED.value,
    ],
    EstimateStatus.SENT.value: [
        EstimateStatus.APPROVED.value,
        EstimateStatus.PARTIALLY_APPROVED.value,
        EstimateStatus.DECLINED.value,
        EstimateStatus.EXPIRED.value,
        EstimateStatus.CANCELLED.value,
    ],
    EstimateStatus.PARTIALLY_APPROVED.value: [
        EstimateStatus.APPROVED.value,
        EstimateStatus.DECLINED.value,
        EstimateStatus.EXPIRED.value,
    ],
    EstimateStatus.APPROVED.value: [EstimateStatus.CANCELLED.value],
    EstimateStatus.DECLINED.value: [],
    EstimateStatus.EXPIRED.value: [],
    EstimateStatus.CANCELLED.value: [],
}


def is_transition_allowed(current: str, new: str) -> bool:
    """Whether an estimate status change is permitted."""
    return new in ESTIMATE_STATUS_TRANSITIONS.get(current, [])


class Estimate(TimestampedBase):
    """A priced proposal for work on a vehicle."""

    __tablename__ = "estimates"

    estimate_number: Mapped[str] = mapped_column(String(30), nullable=False, unique=True, index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vehicle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    inspection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inspections.id", ondelete="SET NULL"), nullable=True, index=True
    )
    service_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_requests.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), default=EstimateStatus.DRAFT.value, nullable=False, index=True
    )
    valid_until: Mapped[datetime | None] = mapped_column(Date, nullable=True)

    subtotal: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False), default=0.0, nullable=False)
    discount_amount: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False), default=0.0, nullable=False
    )
    tax_rate: Mapped[float] = mapped_column(Numeric(6, 4, asdecimal=False), default=0.0, nullable=False)
    tax_amount: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False), default=0.0, nullable=False)
    total: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False), default=0.0, nullable=False)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decline_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list[EstimateItem]] = relationship(
        "EstimateItem",
        back_populates="estimate",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="EstimateItem.sequence, EstimateItem.created_at",
    )

    @property
    def is_expired(self) -> bool:
        """True once ``valid_until`` is in the past."""
        if self.valid_until is None:
            return False
        today = datetime.now(UTC).date()
        return self.valid_until < today

    @property
    def is_open_for_decision(self) -> bool:
        """True when the customer can still approve or decline lines.

        A partially approved estimate stays open: the customer may have
        approved the safety work and still be deciding the optional extras.
        """
        return self.status in AWAITING_DECISION_STATUSES | PARTIAL_DECISION_STATUSES and not self.is_expired

    @property
    def approved_items(self) -> list[EstimateItem]:
        """Lines the customer approved."""
        return [i for i in self.items if i.status == EstimateItemStatus.APPROVED.value]

    @property
    def approved_total(self) -> float:
        """Sum of the approved lines, including tax effect."""
        approved_subtotal = round_money(
            sum(i.line_total for i in self.items if i.is_approved_charge)
        )
        approved_discounts = round_money(
            sum(abs(i.line_total) for i in self.items if i.is_approved_discount)
        )
        approved_taxable = max(approved_subtotal - approved_discounts, 0.0)
        return round_money(approved_taxable + approved_taxable * float(self.tax_rate))

    def __repr__(self) -> str:
        return f"<Estimate({self.estimate_number} {self.status} {self.total})>"


class EstimateItem(TimestampedBase):
    """A single priced line on an estimate."""

    __tablename__ = "estimate_items"

    estimate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("estimates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_type: Mapped[str] = mapped_column(
        String(20), default=EstimateItemType.LABOR.value, nullable=False
    )
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    sequence: Mapped[int] = mapped_column(default=0, nullable=False)

    labor_hours: Mapped[float | None] = mapped_column(Numeric(8, 2, asdecimal=False), nullable=True)
    labor_rate: Mapped[float | None] = mapped_column(Numeric(10, 2, asdecimal=False), nullable=True)

    part_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    part_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    quantity: Mapped[float] = mapped_column(Numeric(10, 2, asdecimal=False), default=1.0, nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False), default=0.0, nullable=False)
    discount_amount: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False), default=0.0, nullable=False
    )

    line_total: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False), default=0.0, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=EstimateItemStatus.PENDING.value, nullable=False, index=True
    )
    customer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_optional: Mapped[bool] = mapped_column(default=False, nullable=False)

    estimate: Mapped[Estimate] = relationship("Estimate", back_populates="items")

    @property
    def is_discount(self) -> bool:
        """True for lines that reduce the estimate."""
        return self.item_type == EstimateItemType.DISCOUNT.value

    @property
    def is_approved_charge(self) -> bool:
        """True when this approved line adds to the estimate."""
        return self.status == EstimateItemStatus.APPROVED.value and not self.is_discount

    @property
    def is_approved_discount(self) -> bool:
        """True when this approved line reduces the estimate."""
        return self.status == EstimateItemStatus.APPROVED.value and self.is_discount

    def compute_line_total(self) -> float:
        """Work out this line's net amount.

        Labor bills hours x rate. Parts, services and fees bill quantity x
        unit price. A DISCOUNT line is stored as a positive amount but always
        subtracted. A per-line ``discount_amount`` then comes off any charge,
        never off another discount.
        """
        if self.is_discount:
            return round_money(-abs(float(self.unit_price or 0.0)))

        if self.item_type == EstimateItemType.LABOR.value:
            gross = float(self.labor_hours or 0.0) * float(self.labor_rate or 0.0)
        else:
            gross = float(self.quantity or 0.0) * float(self.unit_price or 0.0)

        net = gross - float(self.discount_amount or 0.0)
        return round_money(max(net, 0.0))

    def __repr__(self) -> str:
        return f"<EstimateItem({self.item_type}: {self.description} = {self.line_total})>"
