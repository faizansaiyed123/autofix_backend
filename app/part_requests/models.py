"""Part request data models.

A part request is how a technician asks the parts department for a part that a
repair order needs but that is not immediately on hand. It carries a simple
approval workflow: the technician raises a request, parts staff approve or
reject it, and once approved it is fulfilled (staged for the job).

Statuses
--------
``PENDING``    raised, awaiting a parts decision
``APPROVED``   parts staff agreed to source it
``REJECTED``   parts staff declined (wrong part, not available, etc.)
``FULFILLED``  the part is staged for the repair
``CANCELLED``  withdrawn

``REJECTED``, ``FULFILLED`` and ``CANCELLED`` are terminal.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TimestampedBase
from app.repair_orders.models import RepairOrder, RepairTask


class PartRequestStatus(str, enum.Enum):
    """Lifecycle of a part request."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FULFILLED = "FULFILLED"
    CANCELLED = "CANCELLED"


PART_REQUEST_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in PartRequestStatus)

TERMINAL_PART_REQUEST_STATUSES: frozenset[str] = frozenset(
    {
        PartRequestStatus.REJECTED.value,
        PartRequestStatus.FULFILLED.value,
        PartRequestStatus.CANCELLED.value,
    }
)

PART_REQUEST_STATUS_TRANSITIONS: dict[str, list[str]] = {
    PartRequestStatus.PENDING.value: [
        PartRequestStatus.APPROVED.value,
        PartRequestStatus.REJECTED.value,
        PartRequestStatus.CANCELLED.value,
    ],
    PartRequestStatus.APPROVED.value: [
        PartRequestStatus.FULFILLED.value,
        PartRequestStatus.CANCELLED.value,
    ],
    PartRequestStatus.REJECTED.value: [],
    PartRequestStatus.FULFILLED.value: [],
    PartRequestStatus.CANCELLED.value: [],
}


def is_transition_allowed(current: str, new: str) -> bool:
    """Whether a part-request status change is permitted."""
    return new in PART_REQUEST_STATUS_TRANSITIONS.get(current, [])


class PartRequest(TimestampedBase):
    """A technician's request for a part needed by a repair order."""

    __tablename__ = "part_requests"

    repair_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repair_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    repair_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repair_tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    part_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    part_name: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(10, 2, asdecimal=False), default=1.0, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(
        String(20), default=PartRequestStatus.PENDING.value, nullable=False, index=True
    )
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    repair_order: Mapped[RepairOrder] = relationship("RepairOrder")
    repair_task: Mapped[RepairTask | None] = relationship("RepairTask")

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_part_requests_quantity"),
        CheckConstraint(
            "status IN " + str(PART_REQUEST_STATUS_VALUES),
            name="ck_part_requests_status",
        ),
    )

    @property
    def is_terminal(self) -> bool:
        """True when this request will accept no further decisions."""
        return self.status in TERMINAL_PART_REQUEST_STATUSES

    def __repr__(self) -> str:
        return f"<PartRequest({self.part_name} x{self.quantity} = {self.status})>"
