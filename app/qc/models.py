"""Quality control data models.

Quality control (QC) is the last gate before a vehicle goes home. Once a repair
order is ``COMPLETED`` the work is not yet releasable: somebody other than the
technician who did the job has to confirm the work was actually done, the parts
landed, the hours were logged and the repair is photographed.

A quality check is therefore an *attempt*: it belongs to one repair order, is
performed by an inspector, runs a set of verification checks, and ends in a
verdict. A failed check sends the repair order back for rework — the RO returns
to ``IN_PROGRESS`` and the technician fixes the problem. When the work comes back
round a *new* check is raised (attempt 2, 3, ...), so the history of what was
found wrong stays on the record rather than being overwritten.

Statuses
--------
``IN_PROGRESS``  inspection underway
``PASSED``       cleared for delivery (RO moves to ``QC_PASSED``)
``FAILED``       sent back for rework (RO returns to ``IN_PROGRESS``)

Both verdicts are terminal for the check; the repair order's own machine decides
what happens next.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TimestampedBase
from app.repair_orders.models import RepairOrder


class QualityCheckStatus(str, enum.Enum):
    """Lifecycle of a single QC attempt."""

    IN_PROGRESS = "IN_PROGRESS"
    PASSED = "PASSED"
    FAILED = "FAILED"


class QCCheckType(str, enum.Enum):
    """The verification checks an inspector runs before signing off.

    ``WORK_COMPLETED``   every task on the RO is finished
    ``PARTS_RECORDED``   every part the job needed was staged/fulfilled
    ``LABOR_RECORDED``   time was logged against the job
    ``PHOTOS_ATTACHED``  the repair is documented with at least one photo

    Each is auto-evaluated from shop data, but an inspector may override any of
    them (e.g. labour logged on paper, or a documented photo elsewhere).
    """

    WORK_COMPLETED = "WORK_COMPLETED"
    PARTS_RECORDED = "PARTS_RECORDED"
    LABOR_RECORDED = "LABOR_RECORDED"
    PHOTOS_ATTACHED = "PHOTOS_ATTACHED"


QUALITY_CHECK_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in QualityCheckStatus)
QC_CHECK_TYPE_VALUES: tuple[str, ...] = tuple(c.value for c in QCCheckType)

# Verdicts are final: a check that has been decided cannot be reopened, because
# the repair order's next move (rework or delivery) depends on the verdict.
TERMINAL_QC_STATUSES: frozenset[str] = frozenset(
    {
        QualityCheckStatus.PASSED.value,
        QualityCheckStatus.FAILED.value,
    }
)


class QualityCheck(TimestampedBase):
    """One QC attempt against a completed repair order."""

    __tablename__ = "quality_checks"

    repair_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repair_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    inspector_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    status: Mapped[str] = mapped_column(
        String(20), default=QualityCheckStatus.IN_PROGRESS.value, nullable=False, index=True
    )

    # Which pass this is for the RO: 1 on the first inspection, 2 after a
    # failure sent the job back, and so on. Kept on the row so the record is
    # self-describing without counting history at read time.
    attempt_number: Mapped[int] = mapped_column(nullable=False, default=1)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    repair_order: Mapped[RepairOrder] = relationship("RepairOrder")

    checks: Mapped[list[QCCheckItem]] = relationship(
        "QCCheckItem",
        back_populates="quality_check",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="QCCheckItem.created_at",
    )

    photos: Mapped[list[QCPhoto]] = relationship(
        "QCPhoto",
        back_populates="quality_check",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="QCPhoto.created_at",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN " + str(QUALITY_CHECK_STATUS_VALUES),
            name="ck_quality_checks_status",
        ),
        CheckConstraint("attempt_number >= 1", name="ck_quality_checks_attempt_number"),
    )

    @property
    def is_terminal(self) -> bool:
        """True when this attempt has already been decided."""
        return self.status in TERMINAL_QC_STATUSES

    @property
    def failed_blocking_checks(self) -> list[QCCheckItem]:
        """Blocking checks that are currently failing."""
        return [c for c in self.checks if c.blocking and not c.passed]

    @property
    def passed(self) -> bool:
        """True when no blocking check is failing."""
        return not self.failed_blocking_checks

    def __repr__(self) -> str:
        return f"<QualityCheck(RO {self.repair_order_id} attempt {self.attempt_number} = {self.status})>"


class QCCheckItem(TimestampedBase):
    """A single verification line on a quality check."""

    __tablename__ = "qc_check_items"

    quality_check_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quality_checks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    check_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)

    passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Non-blocking checks report on the state of the job but never hold up
    # delivery (photos, for example, are a documentation standard rather than a
    # safety gate).
    blocking: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # False once an inspector has overridden the automatic verdict, so the
    # record shows the value came from a human rather than from shop data.
    auto_verified: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # What the automatic evaluation saw, e.g. "2 labour records, 3.50h".
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    quality_check: Mapped[QualityCheck] = relationship("QualityCheck", back_populates="checks")

    __table_args__ = (
        CheckConstraint(
            "check_type IN " + str(QC_CHECK_TYPE_VALUES),
            name="ck_qc_check_items_check_type",
        ),
    )

    def __repr__(self) -> str:
        return f"<QCCheckItem({self.check_type} = {'PASS' if self.passed else 'FAIL'})>"


class QCPhoto(TimestampedBase):
    """A photo taken by the inspector, documenting the finished work."""

    __tablename__ = "qc_photos"

    quality_check_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quality_checks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    photo_url: Mapped[str] = mapped_column(String(500), nullable=False)
    caption: Mapped[str | None] = mapped_column(String(200), nullable=True)

    quality_check: Mapped[QualityCheck] = relationship("QualityCheck", back_populates="photos")

    def __repr__(self) -> str:
        return f"<QCPhoto({self.id})>"
