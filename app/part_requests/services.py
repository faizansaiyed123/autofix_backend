"""Part request business logic.

Owns the technician -> parts-staff workflow: a technician raises a request
against a repair order, parts staff approve or reject it, and an approved
request is fulfilled once the part is staged for the job.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import BusinessRuleError, NotFoundError
from app.part_requests.models import (
    PART_REQUEST_STATUS_TRANSITIONS,
    PartRequest,
    PartRequestStatus,
    is_transition_allowed,
)
from app.part_requests.schemas import PartRequestCreate, PartRequestUpdate
from app.repair_orders.models import RepairOrder, RepairOrderStatus

logger = logging.getLogger("autofix.part_requests.services")

# Repair-order statuses during which a part may still be requested: once the
# work is authorized a technician can discover a missing part mid-job, but not
# after the order has been completed, delivered or cancelled.
REQUESTABLE_RO_STATUSES = frozenset(
    {
        RepairOrderStatus.APPROVED.value,
        RepairOrderStatus.IN_PROGRESS.value,
        RepairOrderStatus.ON_HOLD.value,
    }
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PartRequestService:
    """Service for the technician-to-parts-staff request workflow."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # --- reads --------------------------------------------------------------

    async def get_by_id(self, request_id: UUID | str) -> PartRequest:
        """Get a part request by ID."""
        result = await self.db.execute(
            select(PartRequest)
            .where(PartRequest.id == str(request_id))
            .execution_options(populate_existing=True)
        )
        request = result.scalar_one_or_none()
        if not request:
            raise NotFoundError(f"Part request with id {request_id} not found")
        return request

    async def list_part_requests(
        self,
        *,
        page: int = 1,
        size: int = 20,
        status: str | None = None,
        repair_order_id: UUID | str | None = None,
        requested_by_id: UUID | str | None = None,
    ) -> tuple[list[PartRequest], int]:
        """List part requests with filtering and pagination."""
        stmt = select(PartRequest)
        count_stmt = select(func.count()).select_from(PartRequest)

        filters = []
        if status:
            filters.append(PartRequest.status == status.upper())
        if repair_order_id:
            filters.append(PartRequest.repair_order_id == str(repair_order_id))
        if requested_by_id:
            filters.append(PartRequest.requested_by_id == str(requested_by_id))

        for condition in filters:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        total = (await self.db.execute(count_stmt)).scalar_one()

        offset = (page - 1) * size
        stmt = stmt.order_by(PartRequest.created_at.desc()).offset(offset).limit(size)
        requests = list((await self.db.execute(stmt)).scalars().all())
        return requests, total

    # --- validation helpers -------------------------------------------------

    async def _resolve_repair_order(self, repair_order_id: UUID | str) -> RepairOrder:
        result = await self.db.execute(
            select(RepairOrder)
            .where(RepairOrder.id == str(repair_order_id))
            .execution_options(populate_existing=True)
        )
        ro = result.scalar_one_or_none()
        if not ro:
            raise NotFoundError(f"Repair order with id {repair_order_id} not found")
        return ro

    @staticmethod
    def _assert_requestable(ro: RepairOrder) -> None:
        """Only request parts while the order is authorized and unfinished."""
        if ro.status not in REQUESTABLE_RO_STATUSES:
            raise BusinessRuleError(
                f"Repair order is {ro.status}; parts can only be requested while the "
                "order is APPROVED, IN_PROGRESS or ON_HOLD"
            )

    async def _resolve_task_for_order(
        self, repair_order_id: UUID | str, repair_task_id: UUID | str
    ) -> None:
        """Assert an optional task belongs to the given repair order."""
        from app.repair_orders.models import RepairTask

        task = (
            await self.db.execute(
                select(RepairTask)
                .where(RepairTask.id == str(repair_task_id))
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if not task:
            raise NotFoundError(f"Task {repair_task_id} not found")
        if str(task.repair_order_id) != str(repair_order_id):
            raise BusinessRuleError(
                f"Task {repair_task_id} does not belong to repair order {repair_order_id}"
            )

    # --- writes -------------------------------------------------------------

    async def create_part_request(
        self, data: PartRequestCreate, requested_by_id: UUID | str | None = None
    ) -> PartRequest:
        """Raise a part request against a repair order."""
        ro = await self._resolve_repair_order(data.repair_order_id)
        self._assert_requestable(ro)

        if data.repair_task_id:
            await self._resolve_task_for_order(data.repair_order_id, data.repair_task_id)

        request = PartRequest(
            repair_order_id=str(data.repair_order_id),
            repair_task_id=(
                str(data.repair_task_id) if data.repair_task_id else None
            ),
            requested_by_id=str(requested_by_id) if requested_by_id else None,
            part_number=data.part_number,
            part_name=data.part_name,
            quantity=float(data.quantity),
            reason=data.reason,
            status=PartRequestStatus.PENDING.value,
        )
        self.db.add(request)
        await self.db.commit()
        await self.db.refresh(request)
        logger.info("Part request raised on RO %s: %s", ro.ro_number, data.part_name)
        return request

    async def update_part_request(
        self, request_id: UUID | str, update_data: PartRequestUpdate
    ) -> PartRequest:
        """Edit a pending part request's details."""
        request = await self.get_by_id(request_id)
        if request.status != PartRequestStatus.PENDING.value:
            raise BusinessRuleError(
                f"Part request is {request.status}; only a PENDING request can be edited"
            )

        changes = update_data.model_dump(exclude_unset=True)
        for field, value in changes.items():
            setattr(request, field, value)

        await self.db.commit()
        await self.db.refresh(request)
        return request

    async def decide_part_request(
        self,
        request_id: UUID | str,
        decision: str,
        decided_by_id: UUID | str | None = None,
        decision_reason: str | None = None,
    ) -> PartRequest:
        """Approve or reject a pending part request (parts staff)."""
        try:
            new_status = PartRequestStatus(str(decision).upper()).value
        except (ValueError, AttributeError):
            allowed = ", ".join(s.value for s in PartRequestStatus)
            raise BusinessRuleError(
                f"Invalid part request decision: {decision}. Allowed: {allowed}"
            )

        if new_status not in (
            PartRequestStatus.APPROVED.value,
            PartRequestStatus.REJECTED.value,
        ):
            raise BusinessRuleError(
                "A decision must be APPROVED or REJECTED"
            )

        request = await self.get_by_id(request_id)
        self._transition(request, new_status)

        if decided_by_id:
            request.decided_by_id = str(decided_by_id)
        if decision_reason is not None:
            request.decision_reason = decision_reason
        request.decided_at = _utcnow()

        await self.db.commit()
        await self.db.refresh(request)
        logger.info("Part request %s -> %s", request_id, new_status)
        return request

    async def fulfil_part_request(
        self, request_id: UUID | str, decided_by_id: UUID | str | None = None
    ) -> PartRequest:
        """Mark an approved part request as staged for the job."""
        request = await self.get_by_id(request_id)
        self._transition(request, PartRequestStatus.FULFILLED.value)
        if decided_by_id:
            request.decided_by_id = str(decided_by_id)
        await self.db.commit()
        await self.db.refresh(request)
        logger.info("Part request %s fulfilled", request_id)
        return request

    async def cancel_part_request(
        self, request_id: UUID | str, reason: str | None = None
    ) -> PartRequest:
        """Withdraw a part request."""
        request = await self.get_by_id(request_id)
        self._transition(request, PartRequestStatus.CANCELLED.value)
        if reason:
            request.decision_reason = reason
        await self.db.commit()
        await self.db.refresh(request)
        return request

    @staticmethod
    def _transition(request: PartRequest, new_status: str) -> None:
        """Apply a status change, enforcing the part-request state machine."""
        if request.status == new_status:
            return
        if not is_transition_allowed(request.status, new_status):
            allowed = PART_REQUEST_STATUS_TRANSITIONS.get(request.status, [])
            raise BusinessRuleError(
                f"Cannot transition part request status from '{request.status}' to "
                f"'{new_status}'. Allowed: {allowed or 'none (terminal state)'}"
            )
        request.status = new_status

    async def delete_part_request(self, request_id: UUID | str) -> None:
        """Delete a pending part request."""
        request = await self.get_by_id(request_id)
        if request.status != PartRequestStatus.PENDING.value:
            raise BusinessRuleError(
                f"Part request is {request.status}; only a PENDING request can be deleted"
            )
        await self.db.delete(request)
        await self.db.commit()
        logger.info("Part request deleted: %s", request_id)
