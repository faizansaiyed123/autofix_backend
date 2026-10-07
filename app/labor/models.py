"""Labor record data models.

A labor record captures time a technician spent on a repair order (optionally
against a specific task). It separates *actual* hours worked from *billable*
hours charged to the customer: a technician may spend 3 hours on a job the
customer is only billed 2 for (diagnostic time, a warranty fix, goodwill).

The billable hours default to the actual hours but can be set independently.
The hourly rate is stored per record so a rate change never silently reprices
historical labor.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TimestampedBase
from app.repair_orders.models import RepairOrder, RepairTask


class LaborRecord(TimestampedBase):
    """Technician time logged against a repair order (and optionally a task)."""

    __tablename__ = "labor_records"

    repair_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repair_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    repair_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repair_tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    technician_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    description: Mapped[str] = mapped_column(String(300), nullable=False)

    actual_hours: Mapped[float] = mapped_column(Numeric(6, 2, asdecimal=False), nullable=False)
    billable_hours: Mapped[float] = mapped_column(
        Numeric(6, 2, asdecimal=False), nullable=False
    )
    hourly_rate: Mapped[float] = mapped_column(
        Numeric(10, 2, asdecimal=False), default=0.0, nullable=False
    )

    performed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    repair_order: Mapped[RepairOrder] = relationship("RepairOrder")
    repair_task: Mapped[RepairTask | None] = relationship("RepairTask")

    __table_args__ = (
        CheckConstraint("actual_hours >= 0", name="ck_labor_records_actual_hours"),
        CheckConstraint("billable_hours >= 0", name="ck_labor_records_billable_hours"),
        CheckConstraint("hourly_rate >= 0", name="ck_labor_records_hourly_rate"),
    )

    @property
    def labor_cost(self) -> float:
        """Amount this record contributes to the repair bill."""
        return round(float(self.billable_hours or 0.0) * float(self.hourly_rate or 0.0), 2)

    def __repr__(self) -> str:
        return f"<LaborRecord({self.description}: {self.actual_hours}h actual / {self.billable_hours}h billable)>"
