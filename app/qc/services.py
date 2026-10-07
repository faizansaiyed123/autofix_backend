"""Quality control business logic.

Owns the QC workflow: raising an attempt against a completed repair order,
auto-verifying the shop's own records (work done, parts staged, labour logged,
photos attached), allowing an inspector to override any automatic verdict, and
recording the verdict — which is what moves the repair order on to delivery or
back for rework.

The verdicts are enforced against real data rather than trusted input: a check
cannot be passed while a blocking verification is failing, and the inspector may
not sign off their own work.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.common.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.qc.models import (
    QCCheckItem,
    QCCheckType,
    QCPhoto,
    QualityCheck,
    QualityCheckStatus,
)
from app.qc.schemas import (
    QCCheckItemOverride,
    QCPhotoCreate,
    QCQueueItem,
    QualityCheckCreate,
    QualityCheckRead,
    QualityCheckUpdate,
)
from app.repair_orders.models import RepairOrder, RepairOrderStatus
from app.repair_orders.services import RepairOrderService

logger = logging.getLogger("autofix.qc.services")

# Part requests still in flight mean the job may be waiting on hardware, so QC
# cannot pass while they are outstanding.
OUTSTANDING_PART_REQUEST_STATUSES = ("PENDING", "APPROVED")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class QualityControlService:
    """Service for quality control attempts and verification checks."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.ros = RepairOrderService(db)

    # --- reads --------------------------------------------------------------

    async def get_by_id(self, quality_check_id: UUID | str) -> QualityCheck:
        """Get a QC attempt with its checks and photos loaded."""
        result = await self.db.execute(
            select(QualityCheck)
            .options(
                selectinload(QualityCheck.checks),
                selectinload(QualityCheck.photos),
            )
            .where(QualityCheck.id == str(quality_check_id))
            .execution_options(populate_existing=True)
        )
        check = result.scalar_one_or_none()
        if not check:
            raise NotFoundError(f"Quality check with id {quality_check_id} not found")
        return check

    async def get_latest_for_order(self, repair_order_id: UUID | str) -> QualityCheck | None:
        """Most recent QC attempt for a repair order, if any."""
        result = await self.db.execute(
            select(QualityCheck)
            .options(
                selectinload(QualityCheck.checks),
                selectinload(QualityCheck.photos),
            )
            .where(QualityCheck.repair_order_id == str(repair_order_id))
            .order_by(QualityCheck.attempt_number.desc(), QualityCheck.created_at.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def list_checks(
        self,
        *,
        page: int = 1,
        size: int = 20,
        status: str | None = None,
        repair_order_id: UUID | str | None = None,
        inspector_id: UUID | str | None = None,
    ) -> tuple[list[QualityCheck], int]:
        """List QC attempts with filtering and pagination."""
        stmt = select(QualityCheck).options(
            selectinload(QualityCheck.checks),
            selectinload(QualityCheck.photos),
        )
        count_stmt = select(func.count()).select_from(QualityCheck)

        filters = []
        if status:
            filters.append(QualityCheck.status == status.upper())
        if repair_order_id:
            filters.append(QualityCheck.repair_order_id == str(repair_order_id))
        if inspector_id:
            filters.append(QualityCheck.inspector_id == str(inspector_id))

        for condition in filters:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        total = (await self.db.execute(count_stmt)).scalar_one()

        offset = (page - 1) * size
        stmt = (
            stmt.order_by(QualityCheck.created_at.desc()).offset(offset).limit(size)
        )
        checks = list((await self.db.execute(stmt)).scalars().all())
        return checks, total

    async def get_queue(self) -> list[QCQueueItem]:
        """Completed repair orders still waiting on quality control.

        An order drops off the queue as soon as an attempt is opened — the work
        is being inspected, so it is no longer unattended — and it never returns
        once QC has passed (the order itself moves to ``QC_PASSED``).
        """
        completed = (
            await self.db.execute(
                select(RepairOrder).where(
                    RepairOrder.status == RepairOrderStatus.COMPLETED.value
                )
            )
        ).scalars().all()

        open_checks = {
            str(checked_ro_id)
            for checked_ro_id in (
                await self.db.execute(
                    select(QualityCheck.repair_order_id).where(
                        QualityCheck.status == QualityCheckStatus.IN_PROGRESS.value
                    )
                )
            ).scalars().all()
        }

        return [
            QCQueueItem(
                repair_order_id=ro.id,
                ro_number=ro.ro_number,
                customer_id=ro.customer_id,
                vehicle_id=ro.vehicle_id,
                technician_id=ro.technician_id,
                completed_at=ro.completed_at,
                has_open_check=str(ro.id) in open_checks,
            )
            for ro in sorted(completed, key=lambda r: r.completed_at or r.created_at)
        ]

    # --- verification -------------------------------------------------------

    async def _outstanding_part_requests(self, ro: RepairOrder) -> list[str]:
        """Part requests on this RO that have not been staged yet."""
        from app.part_requests.models import PartRequest

        rows = (
            await self.db.execute(
                select(PartRequest).where(PartRequest.repair_order_id == str(ro.id))
            )
        ).scalars().all()
        return [
            f"{r.part_name} x{r.quantity:g} ({r.status})"
            for r in rows
            if r.status in OUTSTANDING_PART_REQUEST_STATUSES
        ]

    async def _labor_summary(self, ro: RepairOrder) -> tuple[int, float]:
        """Number of labour records and total actual hours on this RO."""
        from app.labor.models import LaborRecord

        row = (
            await self.db.execute(
                select(
                    func.count(LaborRecord.id),
                    func.coalesce(func.sum(LaborRecord.actual_hours), 0.0),
                ).where(LaborRecord.repair_order_id == str(ro.id))
            )
        ).one()
        return int(row[0] or 0), float(row[1] or 0.0)

    async def _inspection_photo_count(self, ro: RepairOrder) -> int:
        """Photos captured against inspections of this RO's vehicle."""
        from app.inspections.models import Inspection, InspectionPhoto

        return int(
            (
                await self.db.execute(
                    select(func.count(InspectionPhoto.id))
                    .select_from(InspectionPhoto)
                    .join(Inspection, InspectionPhoto.inspection_id == Inspection.id)
                    .where(Inspection.vehicle_id == str(ro.vehicle_id))
                )
            ).scalar_one()
            or 0
        )

    async def _evaluate_checks(
        self, ro: RepairOrder, qc_photo_count: int
    ) -> dict[str, tuple[bool, bool, str]]:
        """Evaluate every verification check against current shop data.

        Returns ``check_type -> (passed, blocking, evidence)``.
        """
        results: dict[str, tuple[bool, bool, str]] = {}

        # --- work completed
        open_tasks = [t for t in ro.tasks if not t.is_done]
        results[QCCheckType.WORK_COMPLETED.value] = (
            bool(ro.tasks) and not open_tasks,
            True,
            (
                f"{len(ro.tasks)} of {len(ro.tasks)} tasks complete"
                if ro.tasks and not open_tasks
                else (
                    "no tasks on the repair order"
                    if not ro.tasks
                    else "still open: " + ", ".join(t.description for t in open_tasks)
                )
            ),
        )

        # --- parts recorded
        outstanding = await self._outstanding_part_requests(ro)
        results[QCCheckType.PARTS_RECORDED.value] = (
            not outstanding,
            True,
            "all requested parts staged"
            if not outstanding
            else "awaiting parts: " + ", ".join(outstanding),
        )

        # --- labour recorded
        labor_count, labor_hours = await self._labor_summary(ro)
        results[QCCheckType.LABOR_RECORDED.value] = (
            labor_count > 0,
            True,
            f"{labor_count} labour record(s), {labor_hours:.2f}h logged",
        )

        # --- photos attached: QC shots, falling back to inspection photos of
        # the same vehicle so an inspection taken at check-in still counts.
        inspection_photos = await self._inspection_photo_count(ro)
        photo_total = qc_photo_count + inspection_photos
        results[QCCheckType.PHOTOS_ATTACHED.value] = (
            photo_total > 0,
            False,  # documentation standard, not a delivery gate
            f"{qc_photo_count} QC photo(s), {inspection_photos} inspection photo(s)",
        )

        return results

    async def _run_verification(self, check: QualityCheck) -> None:
        """Re-evaluate the auto-verified checks on an in-progress attempt.

        Checks an inspector has overridden are left alone: an override exists
        precisely because the shop data cannot answer the question.
        """
        ro = await self.ros.get_by_id(check.repair_order_id)
        results = await self._evaluate_checks(ro, len(check.photos))

        for item in check.checks:
            if not item.auto_verified:
                continue
            outcome = results.get(item.check_type)
            if not outcome:
                continue
            item.passed, _, item.evidence = outcome

        await self.db.commit()

    # --- writes -------------------------------------------------------------

    async def _assert_inspector_valid(self, ro: RepairOrder, inspector_id: UUID | str | None) -> None:
        """An inspector must be a real user, and must not be the technician.

        Self-inspection is the one failure mode QC cannot detect: a technician
        marking their own work as good makes the whole gate decorative.
        """
        if not inspector_id:
            return

        from app.auth.models import User

        user = (
            await self.db.execute(select(User).where(User.id == str(inspector_id)))
        ).scalar_one_or_none()
        if not user:
            raise ConflictError(f"Inspector with id {inspector_id} not found")

        if ro.technician_id and str(ro.technician_id) == str(inspector_id):
            raise BusinessRuleError(
                "The technician who performed the work cannot run quality control "
                "on their own repair order"
            )

    async def create_check(
        self, data: QualityCheckCreate, *, inspector_id: UUID | str | None = None
    ) -> QualityCheck:
        """Start a QC attempt against a completed repair order."""
        ro = await self.ros.get_by_id(data.repair_order_id)

        if ro.status != RepairOrderStatus.COMPLETED.value:
            raise BusinessRuleError(
                f"Repair order is {ro.status}; quality control only starts once the "
                "work is COMPLETED"
            )
        await self._assert_inspector_valid(ro, inspector_id)

        existing = await self.get_latest_for_order(ro.id)
        if existing and existing.status == QualityCheckStatus.IN_PROGRESS.value:
            raise ConflictError(
                "This repair order already has an open quality check; finish it first"
            )
        attempt_number = (existing.attempt_number + 1) if existing else 1

        # Seed one line per verification type, evaluating each against the
        # order's real records before anyone looks at the screen. The items are
        # built up front and handed to the constructor: appending afterwards
        # would touch the still-unloaded relationship and trigger lazy IO.
        results = await self._evaluate_checks(ro, 0)
        items = [
            QCCheckItem(
                check_type=check_type.value,
                passed=results[check_type.value][0],
                blocking=results[check_type.value][1],
                auto_verified=True,
                evidence=results[check_type.value][2],
            )
            for check_type in QCCheckType
        ]

        check = QualityCheck(
            repair_order_id=str(ro.id),
            inspector_id=str(inspector_id) if inspector_id else None,
            status=QualityCheckStatus.IN_PROGRESS.value,
            attempt_number=attempt_number,
            started_at=_utcnow(),
            notes=data.notes,
            checks=items,
        )
        self.db.add(check)
        await self.db.commit()
        logger.info(
            "Quality check started on RO %s (attempt %s)", ro.ro_number, attempt_number
        )
        return await self.get_by_id(check.id)

    async def update_check(
        self, quality_check_id: UUID | str, update_data: QualityCheckUpdate
    ) -> QualityCheck:
        """Update the notes on an in-progress attempt."""
        check = await self._assert_open(quality_check_id)
        for field, value in update_data.model_dump(exclude_unset=True).items():
            setattr(check, field, value)
        await self.db.commit()
        return await self.get_by_id(check.id)

    async def override_check(
        self,
        quality_check_id: UUID | str,
        check_type: str,
        override: QCCheckItemOverride,
    ) -> QualityCheck:
        """Record an inspector's manual verdict on one verification check."""
        check = await self._assert_open(quality_check_id)
        try:
            wanted = QCCheckType(str(check_type).upper()).value
        except ValueError:
            allowed = ", ".join(c.value for c in QCCheckType)
            raise BusinessRuleError(f"Invalid QC check type '{check_type}'. Allowed: {allowed}")

        item = next((c for c in check.checks if c.check_type == wanted), None)
        if not item:
            raise NotFoundError(f"Check {wanted} not present on quality check {quality_check_id}")

        item.passed = override.passed
        item.auto_verified = False
        if override.blocking is not None:
            item.blocking = override.blocking
        if override.notes is not None:
            item.notes = override.notes

        await self.db.commit()
        return await self.get_by_id(check.id)

    async def reverify(self, quality_check_id: UUID | str) -> QualityCheck:
        """Re-run the automatic verification against current shop data."""
        check = await self._assert_open(quality_check_id)
        await self._run_verification(check)
        return await self.get_by_id(check.id)

    async def add_photo(
        self, quality_check_id: UUID | str, photo_data: QCPhotoCreate
    ) -> QualityCheck:
        """Attach a photo to an in-progress attempt and refresh the photo check."""
        check = await self._assert_open(quality_check_id)
        check.photos.append(
            QCPhoto(
                quality_check_id=str(check.id),
                photo_url=photo_data.photo_url,
                caption=photo_data.caption,
            )
        )
        await self.db.commit()

        # A new photo can flip the PHOTOS_ATTACHED verdict, so re-run the
        # automatic checks rather than leaving a stale failure on screen.
        check = await self.get_by_id(check.id)
        await self._run_verification(check)
        return await self.get_by_id(check.id)

    async def delete_photo(self, quality_check_id: UUID | str, photo_id: UUID | str) -> QualityCheck:
        """Remove a photo from an in-progress attempt."""
        check = await self._assert_open(quality_check_id)
        photo = next((p for p in check.photos if str(p.id) == str(photo_id)), None)
        if not photo:
            raise NotFoundError(f"Photo {photo_id} not found on quality check {quality_check_id}")
        check.photos.remove(photo)
        await self.db.commit()

        check = await self.get_by_id(check.id)
        await self._run_verification(check)
        return await self.get_by_id(check.id)

    async def pass_check(
        self, quality_check_id: UUID | str, notes: str | None = None
    ) -> QualityCheck:
        """Record a pass verdict and release the repair order for delivery."""
        check = await self._assert_open(quality_check_id)

        if not check.passed:
            failing = ", ".join(c.check_type for c in check.failed_blocking_checks)
            raise BusinessRuleError(
                f"Cannot pass quality control while blocking checks are failing: {failing}"
            )

        check.status = QualityCheckStatus.PASSED.value
        check.completed_at = _utcnow()
        if notes is not None:
            check.notes = notes

        # The RO service owns the RO status machine and its milestone stamps;
        # its commit lands both the verdict and the status change together.
        await self.ros.update_status(
            check.repair_order_id, RepairOrderStatus.QC_PASSED.value
        )
        logger.info("Quality check %s passed; repair order released for delivery", check.id)
        return await self.get_by_id(check.id)

    async def fail_check(
        self, quality_check_id: UUID | str, reason: str
    ) -> QualityCheck:
        """Record a fail verdict and send the repair order back for rework."""
        if not (reason or "").strip():
            raise BusinessRuleError("A failed quality check must state why it failed")

        check = await self._assert_open(quality_check_id)
        check.status = QualityCheckStatus.FAILED.value
        check.completed_at = _utcnow()
        check.failure_reason = reason

        # Rework: the order goes back to the technician, who reopens whatever
        # task the inspector flagged.
        await self.ros.update_status(
            check.repair_order_id, RepairOrderStatus.IN_PROGRESS.value
        )
        logger.info(
            "Quality check %s failed; repair order returned for rework", check.id
        )
        return await self.get_by_id(check.id)

    async def delete_check(self, quality_check_id: UUID | str) -> None:
        """Discard an in-progress attempt that was raised in error."""
        check = await self._assert_open(quality_check_id)
        await self.db.delete(check)
        await self.db.commit()
        logger.info("Quality check discarded: %s", quality_check_id)

    # --- helpers ------------------------------------------------------------

    async def _assert_open(self, quality_check_id: UUID | str) -> QualityCheck:
        """Load an attempt and assert it has not been decided yet."""
        check = await self.get_by_id(quality_check_id)
        if check.is_terminal:
            raise BusinessRuleError(
                f"Quality check is {check.status} and can no longer be changed; "
                "raise a new attempt instead"
            )
        return check

    @staticmethod
    def to_read(check: QualityCheck) -> QualityCheckRead:
        """Shape a QC attempt for the API."""
        return QualityCheckRead.model_validate(check)


__all__ = [
    "OUTSTANDING_PART_REQUEST_STATUSES",
    "QCCheckType",
    "QualityControlService",
]
