"""Repair order business logic.

Owns repair-order and task CRUD, the RO status machine, and the business rules
that gate them: an RO can only be created from an approved estimate, its task
breakdown freezes once work starts, and an RO cannot be completed while any task
is still open.
"""

from __future__ import annotations

import logging
import uuid as uuid_module
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.common.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.repair_orders.models import (
    REPAIR_ORDER_STATUS_TRANSITIONS,
    RepairOrder,
    RepairOrderStatus,
    RepairTask,
    RepairTaskStatus,
    is_transition_allowed,
)
from app.repair_orders.schemas import (
    RepairOrderCreate,
    RepairOrderSummary,
    RepairOrderTaskCounts,
    RepairOrderUpdate,
    RepairTaskCreate,
    RepairTaskUpdate,
)

logger = logging.getLogger("autofix.repair_orders.services")

# Statuses that block hard deletion: the vehicle has been worked on.
UNDELETABLE_RO_STATUSES = frozenset(
    {
        RepairOrderStatus.IN_PROGRESS.value,
        RepairOrderStatus.ON_HOLD.value,
        RepairOrderStatus.COMPLETED.value,
        RepairOrderStatus.QC_PASSED.value,
        RepairOrderStatus.DELIVERED.value,
    }
)

# Estimate states that represent customer authorization to do the work.
APPROVED_ESTIMATE_STATUSES = frozenset(
    {
        "APPROVED",
        "PARTIALLY_APPROVED",
    }
)

# Task status machine: how a single job may move.
REPAIR_TASK_STATUS_TRANSITIONS: dict[str, list[str]] = {
    RepairTaskStatus.PENDING.value: [
        RepairTaskStatus.IN_PROGRESS.value,
        RepairTaskStatus.COMPLETED.value,
        RepairTaskStatus.SKIPPED.value,
    ],
    RepairTaskStatus.IN_PROGRESS.value: [
        RepairTaskStatus.COMPLETED.value,
        RepairTaskStatus.SKIPPED.value,
        RepairTaskStatus.PENDING.value,
    ],
    RepairTaskStatus.COMPLETED.value: [
        RepairTaskStatus.IN_PROGRESS.value,
        RepairTaskStatus.PENDING.value,
    ],
    RepairTaskStatus.SKIPPED.value: [
        RepairTaskStatus.PENDING.value,
        RepairTaskStatus.IN_PROGRESS.value,
    ],
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def is_task_transition_allowed(current: str, new: str) -> bool:
    """Whether a task status change is permitted."""
    return new in REPAIR_TASK_STATUS_TRANSITIONS.get(current, [])


class RepairOrderService:
    """Service for repair order and task management."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # --- reads --------------------------------------------------------------

    async def get_by_id(self, repair_order_id: UUID | str) -> RepairOrder:
        """Get a repair order by ID with its tasks loaded.

        ``populate_existing`` refreshes identity-mapped instances; sessions run
        with ``expire_on_commit=False``, so a re-read in the same session would
        otherwise hand back a stale task collection after a write.
        """
        result = await self.db.execute(
            select(RepairOrder)
            .options(selectinload(RepairOrder.tasks))
            .where(RepairOrder.id == str(repair_order_id))
            .execution_options(populate_existing=True)
        )
        ro = result.scalar_one_or_none()
        if not ro:
            raise NotFoundError(f"Repair order with id {repair_order_id} not found")
        return ro

    async def get_by_number(self, ro_number: str) -> RepairOrder:
        """Look a repair order up by its human-facing RO number."""
        result = await self.db.execute(
            select(RepairOrder)
            .options(selectinload(RepairOrder.tasks))
            .where(RepairOrder.ro_number == ro_number)
            .execution_options(populate_existing=True)
        )
        ro = result.scalar_one_or_none()
        if not ro:
            raise NotFoundError(f"Repair order {ro_number} not found")
        return ro

    async def list_repair_orders(
        self,
        *,
        page: int = 1,
        size: int = 20,
        status: str | None = None,
        customer_id: UUID | str | None = None,
        vehicle_id: UUID | str | None = None,
        technician_id: UUID | str | None = None,
        advisor_id: UUID | str | None = None,
        estimate_id: UUID | str | None = None,
    ) -> tuple[list[RepairOrder], int]:
        """List repair orders with filtering and pagination."""
        stmt = select(RepairOrder).options(selectinload(RepairOrder.tasks))
        count_stmt = select(func.count()).select_from(RepairOrder)

        filters = []
        if status:
            filters.append(RepairOrder.status == status.upper())
        if customer_id:
            filters.append(RepairOrder.customer_id == str(customer_id))
        if vehicle_id:
            filters.append(RepairOrder.vehicle_id == str(vehicle_id))
        if technician_id:
            filters.append(RepairOrder.technician_id == str(technician_id))
        if advisor_id:
            filters.append(RepairOrder.advisor_id == str(advisor_id))
        if estimate_id:
            filters.append(RepairOrder.estimate_id == str(estimate_id))

        for condition in filters:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        total = (await self.db.execute(count_stmt)).scalar_one()

        offset = (page - 1) * size
        stmt = stmt.order_by(RepairOrder.created_at.desc()).offset(offset).limit(size)
        orders = list((await self.db.execute(stmt)).scalars().all())
        return orders, total

    # --- validation helpers -------------------------------------------------

    async def _assert_related_records_exist(
        self,
        *,
        customer_id: UUID | str,
        vehicle_id: UUID | str,
        estimate_id: UUID | str | None = None,
        appointment_id: UUID | str | None = None,
        advisor_id: UUID | str | None = None,
        technician_id: UUID | str | None = None,
    ) -> None:
        """Verify FK targets exist and belong to the same customer/vehicle."""
        from app.appointments.models import Appointment
        from app.auth.models import User
        from app.customers.models import Customer
        from app.estimates.models import Estimate
        from app.vehicles.models import Vehicle

        customer = (
            await self.db.execute(select(Customer).where(Customer.id == str(customer_id)))
        ).scalar_one_or_none()
        if not customer:
            raise ConflictError(f"Customer with id {customer_id} not found")

        vehicle = (
            await self.db.execute(select(Vehicle).where(Vehicle.id == str(vehicle_id)))
        ).scalar_one_or_none()
        if not vehicle:
            raise ConflictError(f"Vehicle with id {vehicle_id} not found")

        if str(vehicle.customer_id) != str(customer_id):
            raise BusinessRuleError(
                f"Vehicle {vehicle_id} does not belong to customer {customer_id}"
            )

        if estimate_id:
            estimate = (
                await self.db.execute(
                    select(Estimate).where(Estimate.id == str(estimate_id))
                )
            ).scalar_one_or_none()
            if not estimate:
                raise ConflictError(f"Estimate with id {estimate_id} not found")
            # An RO is authorization to work; it can only be raised against an
            # estimate the customer has already agreed to pay for.
            if estimate.status not in APPROVED_ESTIMATE_STATUSES:
                raise BusinessRuleError(
                    f"Estimate {estimate.estimate_number} is {estimate.status}; a repair "
                    "order can only be created from an APPROVED or PARTIALLY_APPROVED estimate"
                )
            if str(estimate.vehicle_id) != str(vehicle_id):
                raise BusinessRuleError(
                    f"Estimate {estimate_id} is for a different vehicle"
                )
            if str(estimate.customer_id) != str(customer_id):
                raise BusinessRuleError(
                    f"Estimate {estimate_id} belongs to a different customer"
                )

        if appointment_id:
            appointment = (
                await self.db.execute(
                    select(Appointment).where(Appointment.id == str(appointment_id))
                )
            ).scalar_one_or_none()
            if not appointment:
                raise ConflictError(f"Appointment with id {appointment_id} not found")

        for user_id, label in ((advisor_id, "Advisor"), (technician_id, "Technician")):
            if not user_id:
                continue
            user = (
                await self.db.execute(select(User).where(User.id == str(user_id)))
            ).scalar_one_or_none()
            if not user:
                raise ConflictError(f"{label} with id {user_id} not found")

    @staticmethod
    def _new_ro_number() -> str:
        """Generate a human-readable, collision-resistant RO number.

        A date prefix makes the number recognisable at the counter; the random
        suffix keeps concurrent creates from colliding, and the column is
        unique so any residual clash surfaces as a 409 rather than silent
        duplication.
        """
        stamp = datetime.now(UTC).strftime("%Y%m")
        suffix = uuid_module.uuid4().hex[:6].upper()
        return f"RO-{stamp}-{suffix}"

    async def _assert_assigned_user_exists(self, user_id: UUID | str, label: str) -> None:
        """Verify an optional assigned-to user reference resolves."""
        if not user_id:
            return
        from app.auth.models import User

        user = (
            await self.db.execute(select(User).where(User.id == str(user_id)))
        ).scalar_one_or_none()
        if not user:
            raise ConflictError(f"{label} with id {user_id} not found")

    @staticmethod
    def _next_sequence(ro: RepairOrder) -> int:
        """Next display position for a task appended to the order."""
        return max((int(t.sequence or 0) for t in ro.tasks), default=-1) + 1

    def _build_task(
        self,
        task_data: RepairTaskCreate,
        sequence: int,
        repair_order_id: str | None = None,
    ) -> RepairTask:
        """Build a task for a repair order (not yet added to the session)."""
        return RepairTask(
            repair_order_id=repair_order_id,
            description=task_data.description,
            assigned_to_id=(
                str(task_data.assigned_to_id) if task_data.assigned_to_id else None
            ),
            notes=task_data.notes,
            sequence=sequence,
        )

    async def _seed_tasks_from_estimate(
        self, estimate_id: UUID | str, ro_id: str
    ) -> list[RepairTask]:
        """Generate a task breakdown from an estimate's approved lines.

        Each approved, billable line becomes a task the technician performs.
        Discount lines and zero-amount lines are skipped: there is nothing to
        do for a discount.
        """
        from app.estimates.models import Estimate, EstimateItemStatus, EstimateItemType

        estimate = (
            await self.db.execute(
                select(Estimate)
                .options(selectinload(Estimate.items))
                .where(Estimate.id == str(estimate_id))
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if not estimate:
            raise ConflictError(f"Estimate with id {estimate_id} not found")

        tasks: list[RepairTask] = []
        seq = 0
        for item in estimate.items:
            if item.status != EstimateItemStatus.APPROVED.value:
                continue
            if item.item_type == EstimateItemType.DISCOUNT.value:
                continue
            if float(item.line_total or 0.0) <= 0:
                continue
            tasks.append(
                RepairTask(
                    repair_order_id=ro_id,
                    description=item.description,
                    sequence=seq,
                )
            )
            seq += 1
        return tasks

    # --- writes -------------------------------------------------------------

    async def create_repair_order(self, order_data: RepairOrderCreate) -> RepairOrder:
        """Create a repair order, optionally seeded with its task breakdown.

        When an ``estimate_id`` is supplied and no explicit tasks are given,
        the task list is generated from the estimate's approved lines. The
        header, every task, and any timestamps land in one transaction.
        """

        await self._assert_related_records_exist(
            customer_id=order_data.customer_id,
            vehicle_id=order_data.vehicle_id,
            estimate_id=order_data.estimate_id,
            appointment_id=order_data.appointment_id,
            advisor_id=order_data.advisor_id,
            technician_id=order_data.technician_id,
        )
        for task in order_data.tasks:
            await self._assert_assigned_user_exists(task.assigned_to_id, "Assigned user")

        ro = RepairOrder(
            ro_number=self._new_ro_number(),
            customer_id=str(order_data.customer_id),
            vehicle_id=str(order_data.vehicle_id),
            estimate_id=(
                str(order_data.estimate_id) if order_data.estimate_id else None
            ),
            appointment_id=(
                str(order_data.appointment_id) if order_data.appointment_id else None
            ),
            advisor_id=str(order_data.advisor_id) if order_data.advisor_id else None,
            technician_id=(
                str(order_data.technician_id) if order_data.technician_id else None
            ),
            odometer_in=order_data.odometer_in,
            odometer_out=order_data.odometer_out,
            bay=order_data.bay,
            promised_at=order_data.promised_at,
            notes=order_data.notes,
            customer_notes=order_data.customer_notes,
            # Seeding the collection through the constructor loads it up front.
            # Appending afterwards would touch the still-unloaded relationship
            # and trigger lazy IO outside the async greenlet context.
            tasks=[
                self._build_task(task_data, index)
                for index, task_data in enumerate(order_data.tasks)
            ],
        )
        self.db.add(ro)
        await self.db.flush()

        # No explicit tasks but an approved estimate: derive the breakdown.
        if not ro.tasks and order_data.estimate_id:
            for task in await self._seed_tasks_from_estimate(order_data.estimate_id, str(ro.id)):
                ro.tasks.append(task)

        await self.db.commit()
        logger.info("Repair order created: %s", ro.ro_number)
        return await self.get_by_id(ro.id)

    async def update_repair_order(
        self, repair_order_id: UUID | str, update_data: RepairOrderUpdate
    ) -> RepairOrder:
        """Update a repair order's header fields."""
        ro = await self.get_by_id(repair_order_id)

        changes = update_data.model_dump(exclude_unset=True)
        if not changes:
            return ro

        await self._assert_related_records_exist(
            customer_id=ro.customer_id,
            vehicle_id=ro.vehicle_id,
            advisor_id=changes.get("advisor_id", ro.advisor_id),
            technician_id=changes.get("technician_id", ro.technician_id),
        )

        for field, value in changes.items():
            setattr(
                ro,
                field,
                str(value) if field.endswith("_id") and value is not None else value,
            )

        await self.db.commit()
        return await self.get_by_id(repair_order_id)

    # --- tasks --------------------------------------------------------------

    @staticmethod
    def _assert_tasks_editable(ro: RepairOrder) -> None:
        """Reject task breakdown edits once the work has started."""
        if not ro.tasks_editable:
            raise BusinessRuleError(
                f"Repair order is {ro.status}; the task breakdown can only be edited "
                "while the order is DRAFT or APPROVED"
            )

    async def add_task(
        self, repair_order_id: UUID | str, task_data: RepairTaskCreate
    ) -> RepairOrder:
        """Append a task to a repair order's breakdown."""
        ro = await self.get_by_id(repair_order_id)
        self._assert_tasks_editable(ro)
        await self._assert_assigned_user_exists(task_data.assigned_to_id, "Assigned user")

        task = self._build_task(
            task_data, self._next_sequence(ro), repair_order_id=str(ro.id)
        )
        ro.tasks.append(task)

        await self.db.commit()
        return await self.get_by_id(repair_order_id)

    async def update_task(
        self,
        repair_order_id: UUID | str,
        task_id: UUID | str,
        update_data: RepairTaskUpdate,
    ) -> RepairOrder:
        """Update a task's description, assignee or ordering."""
        ro = await self.get_by_id(repair_order_id)

        task = next((t for t in ro.tasks if str(t.id) == str(task_id)), None)
        if not task:
            raise NotFoundError(f"Task {task_id} not found on repair order {repair_order_id}")

        changes = update_data.model_dump(exclude_unset=True)

        # Assigning a technician is operational bookkeeping and stays editable
        # while the work is live — a task is often assigned after the order has
        # started. The breakdown itself (description, ordering) still freezes.
        structural = {f: v for f, v in changes.items() if f != "assigned_to_id"}
        if structural and not ro.tasks_editable:
            raise BusinessRuleError(
                f"Repair order is {ro.status}; the task breakdown can only be edited "
                "while the order is DRAFT or APPROVED"
            )
        if "assigned_to_id" in changes and ro.is_terminal:
            raise BusinessRuleError(
                f"Repair order is {ro.status} and no longer accepts task updates"
            )

        if changes.get("assigned_to_id") is not None:
            await self._assert_assigned_user_exists(
                changes["assigned_to_id"], "Assigned user"
            )

        for field, value in changes.items():
            setattr(
                task,
                field,
                str(value) if field.endswith("_id") and value is not None else value,
            )

        await self.db.commit()
        return await self.get_by_id(repair_order_id)

    async def delete_task(self, repair_order_id: UUID | str, task_id: UUID | str) -> RepairOrder:
        """Remove a task from a repair order's breakdown."""
        ro = await self.get_by_id(repair_order_id)
        self._assert_tasks_editable(ro)

        task = next((t for t in ro.tasks if str(t.id) == str(task_id)), None)
        if not task:
            raise NotFoundError(f"Task {task_id} not found on repair order {repair_order_id}")

        # Removing from the collection keeps in-session state consistent; the
        # delete-orphan cascade issues the DELETE.
        ro.tasks.remove(task)
        await self.db.commit()
        return await self.get_by_id(repair_order_id)

    # --- status machine -----------------------------------------------------

    async def update_status(
        self,
        repair_order_id: UUID | str,
        new_status: str,
        reason: str | None = None,
    ) -> RepairOrder:
        """Transition a repair order's status, enforcing the status machine."""
        try:
            new_status = RepairOrderStatus(str(new_status).upper()).value
        except (ValueError, AttributeError):
            allowed = ", ".join(s.value for s in RepairOrderStatus)
            raise BusinessRuleError(
                f"Invalid repair order status: {new_status}. Allowed: {allowed}"
            )

        ro = await self.get_by_id(repair_order_id)
        if ro.status == new_status:
            return ro

        if not is_transition_allowed(ro.status, new_status):
            allowed = REPAIR_ORDER_STATUS_TRANSITIONS.get(ro.status, [])
            raise BusinessRuleError(
                f"Cannot transition repair order status from '{ro.status}' to "
                f"'{new_status}'. Allowed: {allowed or 'none (terminal state)'}"
            )

        if new_status == RepairOrderStatus.IN_PROGRESS.value:
            self._assert_startable(ro)
        if new_status == RepairOrderStatus.COMPLETED.value:
            self._assert_completable(ro)

        if new_status == RepairOrderStatus.CANCELLED.value and reason:
            ro.cancel_reason = reason

        ro.status = new_status

        # Stamp the lifecycle milestones as the RO passes through them.
        if new_status == RepairOrderStatus.IN_PROGRESS.value and ro.started_at is None:
            ro.started_at = _utcnow()
        if new_status == RepairOrderStatus.COMPLETED.value and ro.completed_at is None:
            ro.completed_at = _utcnow()
        if new_status == RepairOrderStatus.DELIVERED.value:
            ro.delivered_at = _utcnow()

        await self.db.commit()
        logger.info("Repair order %s -> %s", ro.ro_number, new_status)
        return await self.get_by_id(repair_order_id)

    @staticmethod
    def _assert_startable(ro: RepairOrder) -> None:
        """An RO can only start work once it has a task breakdown."""
        if not ro.tasks:
            raise BusinessRuleError(
                "Cannot start a repair order with no tasks; break the work down first"
            )

    @staticmethod
    def _assert_completable(ro: RepairOrder) -> None:
        """An RO can only complete once every task is finished."""
        if not ro.tasks:
            raise BusinessRuleError(
                "Cannot complete a repair order with no tasks"
            )
        if not ro.all_tasks_done:
            open_tasks = [t.description for t in ro.tasks if not t.is_done]
            raise BusinessRuleError(
                "Cannot complete a repair order while tasks are still open: "
                + ", ".join(open_tasks)
            )

    async def update_task_status(
        self,
        repair_order_id: UUID | str,
        task_id: UUID | str,
        new_status: str,
        notes: str | None = None,
    ) -> RepairOrder:
        """Transition a single task's status."""
        try:
            new_status = RepairTaskStatus(str(new_status).upper()).value
        except (ValueError, AttributeError):
            allowed = ", ".join(s.value for s in RepairTaskStatus)
            raise BusinessRuleError(f"Invalid task status: {new_status}. Allowed: {allowed}")

        ro = await self.get_by_id(repair_order_id)
        if ro.is_terminal:
            raise BusinessRuleError(
                f"Repair order is {ro.status} and no longer accepts task updates"
            )

        task = next((t for t in ro.tasks if str(t.id) == str(task_id)), None)
        if not task:
            raise NotFoundError(f"Task {task_id} not found on repair order {repair_order_id}")

        if task.status == new_status:
            return ro

        if not is_task_transition_allowed(task.status, new_status):
            allowed = REPAIR_TASK_STATUS_TRANSITIONS.get(task.status, [])
            raise BusinessRuleError(
                f"Cannot transition task status from '{task.status}' to '{new_status}'. "
                f"Allowed: {allowed or 'none (terminal state)'}"
            )

        task.status = new_status
        if notes is not None:
            task.notes = notes
        if new_status == RepairTaskStatus.COMPLETED.value:
            task.completed_at = _utcnow()
        else:
            task.completed_at = None

        await self.db.commit()
        logger.info("Repair order %s task '%s' -> %s", ro.ro_number, task.description, new_status)
        return await self.get_by_id(repair_order_id)

    # --- summary / deletion -------------------------------------------------

    async def get_summary(self, repair_order_id: UUID | str) -> RepairOrderSummary:
        """Build the compact operational summary for a repair order."""
        ro = await self.get_by_id(repair_order_id)
        counts = RepairOrderTaskCounts(
            total=len(ro.tasks),
            pending=sum(1 for t in ro.tasks if t.status == RepairTaskStatus.PENDING.value),
            in_progress=sum(
                1 for t in ro.tasks if t.status == RepairTaskStatus.IN_PROGRESS.value
            ),
            completed=sum(
                1 for t in ro.tasks if t.status == RepairTaskStatus.COMPLETED.value
            ),
            skipped=sum(1 for t in ro.tasks if t.status == RepairTaskStatus.SKIPPED.value),
        )
        return RepairOrderSummary(
            repair_order_id=ro.id,
            ro_number=ro.ro_number,
            status=ro.status,
            is_terminal=ro.is_terminal,
            tasks_editable=ro.tasks_editable,
            all_tasks_done=ro.all_tasks_done,
            counts=counts,
        )

    async def delete_repair_order(self, repair_order_id: UUID | str) -> None:
        """Delete a repair order that has not been worked on."""
        ro = await self.get_by_id(repair_order_id)
        if ro.status in UNDELETABLE_RO_STATUSES:
            raise BusinessRuleError(
                f"Repair order is {ro.status} and can no longer be deleted; "
                "cancel it instead"
            )
        await self.db.delete(ro)
        await self.db.commit()
        logger.info("Repair order deleted: %s", ro.ro_number)
