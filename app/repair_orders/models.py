"""Repair order data models.

A repair order (RO) is the shop's authorization to work on a vehicle. It is
created from an approved estimate and broken into tasks (the individual jobs a
technician performs). It owns its own lifecycle, independent of the estimate it
came from: an estimate can be approved and still sit unscheduled for days, while
the RO is what the service bay, the technician and the invoice all hang off.

Money is deliberately *not* duplicated onto the RO. The estimate remains the
single source of truth for pricing; the RO tracks execution (which tasks were
done, when work started and finished). Invoice generation in a later phase reads
both. Copying totals here would create a second number that silently drifts.

Statuses
--------
``DRAFT``        created, not yet authorized for work
``APPROVED``     customer authorized the work
``IN_PROGRESS``  a technician has started
``ON_HOLD``      paused (waiting on parts, customer decision, etc.)
``COMPLETED``    all tasks finished
``QC_PASSED``    passed quality control (Phase 10)
``DELIVERED``    handed back to the customer
``CANCELLED``    abandoned before delivery

A ``COMPLETED`` order can go back to ``IN_PROGRESS`` when quality control fails
it, and a ``QC_PASSED`` order can go back to ``COMPLETED`` if the inspector and
the technician disagree afterwards. Both are rework loops driven by Phase 10.

The terminal states (``DELIVERED``, ``CANCELLED``) accept no further
transitions; everything else moves through the machine enforced in the service.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TimestampedBase


class RepairOrderStatus(str, enum.Enum):
    """Lifecycle of a repair order."""

    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    IN_PROGRESS = "IN_PROGRESS"
    ON_HOLD = "ON_HOLD"
    COMPLETED = "COMPLETED"
    QC_PASSED = "QC_PASSED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


class RepairTaskStatus(str, enum.Enum):
    """Lifecycle of a single task on a repair order."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"


# Statuses that mean the RO is finished with and cannot change again.
TERMINAL_RO_STATUSES: frozenset[str] = frozenset(
    {
        RepairOrderStatus.DELIVERED.value,
        RepairOrderStatus.CANCELLED.value,
    }
)

# Statuses whose tasks may still be edited by staff.
TASK_EDITABLE_RO_STATUSES: frozenset[str] = frozenset(
    {
        RepairOrderStatus.DRAFT.value,
        RepairOrderStatus.APPROVED.value,
    }
)

# Statuses that mean the vehicle is actively being worked on.
ACTIVE_RO_STATUSES: frozenset[str] = frozenset(
    {
        RepairOrderStatus.IN_PROGRESS.value,
        RepairOrderStatus.ON_HOLD.value,
    }
)

# Task statuses that count as "finished" for completion checks.
DONE_TASK_STATUSES: frozenset[str] = frozenset(
    {
        RepairTaskStatus.COMPLETED.value,
        RepairTaskStatus.SKIPPED.value,
    }
)

REPAIR_ORDER_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in RepairOrderStatus)
REPAIR_TASK_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in RepairTaskStatus)

REPAIR_ORDER_STATUS_TRANSITIONS: dict[str, list[str]] = {
    RepairOrderStatus.DRAFT.value: [
        RepairOrderStatus.APPROVED.value,
        RepairOrderStatus.CANCELLED.value,
    ],
    RepairOrderStatus.APPROVED.value: [
        RepairOrderStatus.IN_PROGRESS.value,
        RepairOrderStatus.CANCELLED.value,
    ],
    RepairOrderStatus.IN_PROGRESS.value: [
        RepairOrderStatus.ON_HOLD.value,
        RepairOrderStatus.COMPLETED.value,
        RepairOrderStatus.CANCELLED.value,
    ],
    RepairOrderStatus.ON_HOLD.value: [
        RepairOrderStatus.IN_PROGRESS.value,
        RepairOrderStatus.CANCELLED.value,
    ],
    RepairOrderStatus.COMPLETED.value: [
        RepairOrderStatus.QC_PASSED.value,
        # Rework: quality control failed the job (Phase 10), so the order goes
        # back to the technician with the defect recorded against it.
        RepairOrderStatus.IN_PROGRESS.value,
        RepairOrderStatus.CANCELLED.value,
    ],
    RepairOrderStatus.QC_PASSED.value: [
        RepairOrderStatus.DELIVERED.value,
        RepairOrderStatus.COMPLETED.value,
    ],
    RepairOrderStatus.DELIVERED.value: [],
    RepairOrderStatus.CANCELLED.value: [],
}


def is_transition_allowed(current: str, new: str) -> bool:
    """Whether a repair-order status change is permitted."""
    return new in REPAIR_ORDER_STATUS_TRANSITIONS.get(current, [])


class RepairOrder(TimestampedBase):
    """Authorization and execution record for work on a vehicle."""

    __tablename__ = "repair_orders"

    ro_number: Mapped[str] = mapped_column(String(30), nullable=False, unique=True, index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vehicle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    estimate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("estimates.id", ondelete="SET NULL"), nullable=True, index=True
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True
    )
    advisor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    technician_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    status: Mapped[str] = mapped_column(
        String(20), default=RepairOrderStatus.DRAFT.value, nullable=False, index=True
    )

    odometer_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    odometer_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bay: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)

    promised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    tasks: Mapped[list[RepairTask]] = relationship(
        "RepairTask",
        back_populates="repair_order",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="RepairTask.sequence, RepairTask.created_at",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN " + str(REPAIR_ORDER_STATUS_VALUES),
            name="ck_repair_orders_status",
        ),
        CheckConstraint(
            "odometer_in IS NULL OR odometer_in >= 0", name="ck_repair_orders_odometer_in"
        ),
        CheckConstraint(
            "odometer_out IS NULL OR odometer_out >= 0", name="ck_repair_orders_odometer_out"
        ),
    )

    @property
    def is_terminal(self) -> bool:
        """True when the RO can no longer change status."""
        return self.status in TERMINAL_RO_STATUSES

    @property
    def tasks_editable(self) -> bool:
        """True when the task breakdown may still be edited."""
        return self.status in TASK_EDITABLE_RO_STATUSES

    @property
    def all_tasks_done(self) -> bool:
        """True when every task is COMPLETED or SKIPPED.

        An RO with no tasks is not "done": there is nothing to have finished,
        so completion is blocked until the work is actually broken down.
        """
        if not self.tasks:
            return False
        return all(t.status in DONE_TASK_STATUSES for t in self.tasks)

    def __repr__(self) -> str:
        return f"<RepairOrder({self.ro_number} {self.status})>"


class RepairTask(TimestampedBase):
    """A single job on a repair order."""

    __tablename__ = "repair_tasks"

    repair_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repair_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=RepairTaskStatus.PENDING.value, nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN " + str(REPAIR_TASK_STATUS_VALUES),
            name="ck_repair_tasks_status",
        ),
    )

    repair_order: Mapped[RepairOrder] = relationship("RepairOrder", back_populates="tasks")

    @property
    def is_done(self) -> bool:
        """True when this task is finished or intentionally skipped."""
        return self.status in DONE_TASK_STATUSES

    def __repr__(self) -> str:
        return f"<RepairTask({self.description} = {self.status})>"
