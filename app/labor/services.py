"""Labor tracking business logic.

Owns labor-record CRUD against repair orders (and tasks), the gate that stops
time being logged once a repair order is finished, and the technician dashboard
aggregate.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.labor.models import LaborRecord
from app.labor.schemas import (
    LaborRecordCreate,
    LaborRecordUpdate,
    TechnicianDashboard,
    TechnicianDashboardOpenTask,
)
from app.repair_orders.models import RepairOrder, RepairOrderStatus, RepairTask, RepairTaskStatus

logger = logging.getLogger("autofix.labor.services")

# Repair-order statuses during which technician time may be logged. Labor is
# work performed, so it is only valid while the order is actually being worked
# on — never before it starts and never after it finishes.
LABOR_LOGGABLE_RO_STATUSES = frozenset(
    {
        RepairOrderStatus.IN_PROGRESS.value,
        RepairOrderStatus.ON_HOLD.value,
    }
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class LaborService:
    """Service for labor records and technician workload."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # --- reads --------------------------------------------------------------

    async def get_by_id(self, labor_id: UUID | str) -> LaborRecord:
        """Get a labor record by ID."""
        result = await self.db.execute(
            select(LaborRecord)
            .where(LaborRecord.id == str(labor_id))
            .execution_options(populate_existing=True)
        )
        record = result.scalar_one_or_none()
        if not record:
            raise NotFoundError(f"Labor record with id {labor_id} not found")
        return record

    async def list_labor_records(
        self,
        *,
        page: int = 1,
        size: int = 20,
        repair_order_id: UUID | str | None = None,
        repair_task_id: UUID | str | None = None,
        technician_id: UUID | str | None = None,
    ) -> tuple[list[LaborRecord], int]:
        """List labor records with filtering and pagination."""
        stmt = select(LaborRecord)
        count_stmt = select(func.count()).select_from(LaborRecord)

        filters = []
        if repair_order_id:
            filters.append(LaborRecord.repair_order_id == str(repair_order_id))
        if repair_task_id:
            filters.append(LaborRecord.repair_task_id == str(repair_task_id))
        if technician_id:
            filters.append(LaborRecord.technician_id == str(technician_id))

        for condition in filters:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        total = (await self.db.execute(count_stmt)).scalar_one()

        offset = (page - 1) * size
        stmt = stmt.order_by(LaborRecord.performed_at.desc()).offset(offset).limit(size)
        records = list((await self.db.execute(stmt)).scalars().all())
        return records, total

    # --- validation helpers -------------------------------------------------

    async def _resolve_repair_order(self, repair_order_id: UUID | str) -> RepairOrder:
        """Load the parent repair order, asserting it exists."""
        result = await self.db.execute(
            select(RepairOrder)
            .options()
            .where(RepairOrder.id == str(repair_order_id))
            .execution_options(populate_existing=True)
        )
        ro = result.scalar_one_or_none()
        if not ro:
            raise NotFoundError(f"Repair order with id {repair_order_id} not found")
        return ro

    @staticmethod
    def _assert_labor_loggable(ro: RepairOrder) -> None:
        """Only log time while the repair order is actively being worked on."""
        if ro.status not in LABOR_LOGGABLE_RO_STATUSES:
            raise BusinessRuleError(
                f"Repair order is {ro.status}; labor can only be recorded while the "
                "order is IN_PROGRESS or ON_HOLD"
            )

    async def _resolve_task_for_order(
        self, repair_order_id: UUID | str, repair_task_id: UUID | str
    ) -> RepairTask:
        """Load a task and assert it belongs to the given repair order."""
        result = await self.db.execute(
            select(RepairTask)
            .where(RepairTask.id == str(repair_task_id))
            .execution_options(populate_existing=True)
        )
        task = result.scalar_one_or_none()
        if not task:
            raise NotFoundError(f"Task {repair_task_id} not found")
        if str(task.repair_order_id) != str(repair_order_id):
            raise BusinessRuleError(
                f"Task {repair_task_id} does not belong to repair order {repair_order_id}"
            )
        return task

    async def _assert_technician_exists(self, technician_id: UUID | str) -> None:
        """Verify an optional technician reference resolves."""
        if not technician_id:
            return
        from app.auth.models import User

        user = (
            await self.db.execute(select(User).where(User.id == str(technician_id)))
        ).scalar_one_or_none()
        if not user:
            raise ConflictError(f"Technician with id {technician_id} not found")

    # --- writes -------------------------------------------------------------

    async def create_labor_record(self, data: LaborRecordCreate) -> LaborRecord:
        """Log technician time against a repair order (and optionally a task)."""
        ro = await self._resolve_repair_order(data.repair_order_id)
        self._assert_labor_loggable(ro)

        if data.repair_task_id:
            await self._resolve_task_for_order(data.repair_order_id, data.repair_task_id)
        await self._assert_technician_exists(data.technician_id)

        # Billable defaults to actual (handled in the schema validator); if the
        # caller left it unset at the service layer, fall back to actual again.
        billable = (
            float(data.billable_hours)
            if data.billable_hours is not None
            else float(data.actual_hours)
        )

        record = LaborRecord(
            repair_order_id=str(data.repair_order_id),
            repair_task_id=(
                str(data.repair_task_id) if data.repair_task_id else None
            ),
            technician_id=(
                str(data.technician_id) if data.technician_id else None
            ),
            description=data.description,
            actual_hours=float(data.actual_hours),
            billable_hours=billable,
            hourly_rate=float(data.hourly_rate),
            performed_at=data.performed_at or _utcnow(),
            notes=data.notes,
        )
        self.db.add(record)
        await self.db.commit()
        await self.db.refresh(record)
        logger.info(
            "Labor recorded on RO %s: %sh", ro.ro_number, data.actual_hours
        )
        return record

    async def update_labor_record(
        self, labor_id: UUID | str, update_data: LaborRecordUpdate
    ) -> LaborRecord:
        """Update a labor record; only while its order is still being worked on."""
        record = await self.get_by_id(labor_id)
        ro = await self._resolve_repair_order(record.repair_order_id)
        self._assert_labor_loggable(ro)

        changes = update_data.model_dump(exclude_unset=True)
        if not changes:
            return record

        await self._assert_technician_exists(changes.get("technician_id"))

        for field, value in changes.items():
            if field == "technician_id" and value is not None:
                value = str(value)
            setattr(record, field, value)

        await self.db.commit()
        await self.db.refresh(record)
        return record

    async def delete_labor_record(self, labor_id: UUID | str) -> None:
        """Delete a labor record; only while its order is still being worked on."""
        record = await self.get_by_id(labor_id)
        ro = await self._resolve_repair_order(record.repair_order_id)
        self._assert_labor_loggable(ro)
        await self.db.delete(record)
        await self.db.commit()
        logger.info("Labor record deleted: %s", labor_id)

    # --- dashboard ----------------------------------------------------------

    async def get_dashboard(self, technician_id: UUID | str) -> TechnicianDashboard:
        """Build the aggregate workload view for a technician.

        Only work that is genuinely still open is counted: tasks not yet
        completed or skipped, and repair orders still in progress or on hold.
        """
        from app.part_requests.models import PartRequest, PartRequestStatus

        tid = str(technician_id)

        # Open tasks: assigned to this technician and not yet finished, on
        # repair orders that are still being worked on.
        task_rows = (
            await self.db.execute(
                select(RepairTask, RepairOrder)
                .join(RepairOrder, RepairTask.repair_order_id == RepairOrder.id)
                .where(RepairTask.assigned_to_id == tid)
                .where(RepairTask.status.in_(
                    [RepairTaskStatus.PENDING.value, RepairTaskStatus.IN_PROGRESS.value]
                ))
                .where(RepairOrder.status.in_(list(LABOR_LOGGABLE_RO_STATUSES)))
            )
        ).all()

        open_tasks = [
            TechnicianDashboardOpenTask(
                task_id=task.id,
                description=task.description,
                repair_order_id=ro.id,
                ro_number=ro.ro_number,
                status=task.status,
            )
            for task, ro in task_rows
        ]

        in_progress_ro_count = (
            await self.db.execute(
                select(func.count())
                .select_from(RepairOrder)
                .where(RepairOrder.technician_id == tid)
                .where(RepairOrder.status.in_(list(LABOR_LOGGABLE_RO_STATUSES)))
            )
        ).scalar_one()

        # Hours logged in the trailing seven days.
        week_start = _utcnow() - timedelta(days=7)
        hours_this_week = (
            await self.db.execute(
                select(func.coalesce(func.sum(LaborRecord.actual_hours), 0.0)).where(
                    LaborRecord.technician_id == tid,
                    LaborRecord.performed_at >= week_start,
                )
            )
        ).scalar_one()

        pending_part_requests = (
            await self.db.execute(
                select(func.count())
                .select_from(PartRequest)
                .where(PartRequest.requested_by_id == tid)
                .where(PartRequest.status == PartRequestStatus.PENDING.value)
            )
        ).scalar_one()

        return TechnicianDashboard(
            technician_id=uuid.UUID(tid) if isinstance(technician_id, str) else technician_id,
            open_task_count=len(open_tasks),
            in_progress_ro_count=in_progress_ro_count,
            hours_this_week=float(hours_this_week or 0.0),
            pending_part_request_count=pending_part_requests,
            open_tasks=open_tasks,
        )
