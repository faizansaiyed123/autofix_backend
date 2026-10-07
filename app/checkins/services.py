"""Check-in management business logic.

Handles check-in CRUD operations, status management,
and vehicle condition capture.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.checkins.models import CheckIn, CheckInStatus
from app.checkins.schemas import CheckInCreate, CheckInUpdate
from app.common.exceptions import BusinessRuleError, ConflictError, NotFoundError

logger = logging.getLogger("autofix.checkins.services")

VALID_STATUS_TRANSITIONS: dict[str, list[str]] = {
    CheckInStatus.PENDING.value: [CheckInStatus.IN_PROGRESS.value, CheckInStatus.CANCELLED.value],
    CheckInStatus.IN_PROGRESS.value: [CheckInStatus.COMPLETED.value, CheckInStatus.CANCELLED.value],
    CheckInStatus.COMPLETED.value: [],
    CheckInStatus.CANCELLED.value: [],
}


def _validate_status_transition(current: str, new: str) -> None:
    """Validate that a check-in status transition is allowed."""
    allowed = VALID_STATUS_TRANSITIONS.get(current, [])
    if new not in allowed:
        raise BusinessRuleError(
            f"Cannot transition check-in status from '{current}' to '{new}'. Allowed: {allowed}"
        )


class CheckInService:
    """Service for check-in management operations."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, checkin_id: UUID | str) -> CheckIn:
        """Get a check-in by ID."""
        result = await self.db.execute(select(CheckIn).where(CheckIn.id == str(checkin_id)))
        checkin = result.scalar_one_or_none()
        if not checkin:
            raise NotFoundError(f"Check-in with id {checkin_id} not found")
        return checkin

    async def list_checkins(
        self,
        *,
        page: int = 1,
        size: int = 20,
        status: str | None = None,
        vehicle_id: UUID | str | None = None,
        customer_id: UUID | str | None = None,
    ) -> tuple[list[CheckIn], int]:
        """List check-ins with filtering and pagination."""
        stmt = select(CheckIn)
        count_stmt = select(func.count()).select_from(CheckIn)

        if status:
            stmt = stmt.where(CheckIn.status == status)
            count_stmt = count_stmt.where(CheckIn.status == status)

        if vehicle_id:
            stmt = stmt.where(CheckIn.vehicle_id == str(vehicle_id))
            count_stmt = count_stmt.where(CheckIn.vehicle_id == str(vehicle_id))

        if customer_id:
            stmt = stmt.where(CheckIn.customer_id == str(customer_id))
            count_stmt = count_stmt.where(CheckIn.customer_id == str(customer_id))

        count_result = await self.db.execute(count_stmt)
        total = count_result.scalar_one()

        offset = (page - 1) * size
        stmt = stmt.order_by(CheckIn.created_at.desc()).offset(offset).limit(size)
        result = await self.db.execute(stmt)
        checkins = result.scalars().all()

        return checkins, total

    async def create_checkin(self, checkin_data: CheckInCreate) -> CheckIn:
        """Create a new vehicle check-in."""
        from app.customers.models import Customer
        from app.vehicles.models import Vehicle

        # Validate customer exists
        result = await self.db.execute(select(Customer).where(Customer.id == str(checkin_data.customer_id)))
        if not result.scalar_one_or_none():
            raise ConflictError(f"Customer with id {checkin_data.customer_id} not found")

        # Validate vehicle exists
        result = await self.db.execute(select(Vehicle).where(Vehicle.id == str(checkin_data.vehicle_id)))
        if not result.scalar_one_or_none():
            raise ConflictError(f"Vehicle with id {checkin_data.vehicle_id} not found")

        # Validate service advisor exists if provided
        if checkin_data.service_advisor_id:
            from app.auth.models import User

            result = await self.db.execute(select(User).where(User.id == str(checkin_data.service_advisor_id)))
            if not result.scalar_one_or_none():
                raise ConflictError(f"User with id {checkin_data.service_advisor_id} not found")

        checkin = CheckIn(
            customer_id=str(checkin_data.customer_id),
            vehicle_id=str(checkin_data.vehicle_id),
            odometer=checkin_data.odometer,
            checkin_type=checkin_data.checkin_type,
            notes=checkin_data.notes,
            expected_completion=checkin_data.expected_completion,
            tire_condition=checkin_data.tire_condition,
            fluid_levels=checkin_data.fluid_levels,
            lights_status=checkin_data.lights_status,
            service_advisor_id=str(checkin_data.service_advisor_id) if checkin_data.service_advisor_id else None,
        )

        self.db.add(checkin)
        await self.db.commit()
        await self.db.refresh(checkin)
        logger.info("Check-in created: %s", checkin.id)
        return checkin

    async def update_checkin(self, checkin_id: UUID | str, update_data: CheckInUpdate) -> CheckIn:
        """Update an existing check-in."""
        checkin = await self.get_by_id(checkin_id)

        data = update_data.model_dump(exclude_unset=True)

        if "status" in data and data["status"] is not None:
            _validate_status_transition(checkin.status, data["status"])

        for field, value in data.items():
            if value is not None:
                setattr(checkin, field, value)

        await self.db.commit()
        await self.db.refresh(checkin)
        logger.info("Check-in updated: %s", checkin.id)
        return checkin

    async def update_status(self, checkin_id: UUID | str, status: str) -> CheckIn:
        """Update check-in status with transition validation."""
        try:
            CheckInStatus(status)
        except ValueError:
            raise BusinessRuleError(f"Invalid check-in status: {status}")

        checkin = await self.get_by_id(checkin_id)
        _validate_status_transition(checkin.status, status)
        checkin.status = status
        await self.db.commit()
        await self.db.refresh(checkin)
        logger.info("Check-in %s status -> %s", checkin.id, status)
        return checkin

    async def delete_checkin(self, checkin_id: UUID | str) -> None:
        """Delete a check-in."""
        checkin = await self.get_by_id(checkin_id)
        await self.db.delete(checkin)
        await self.db.commit()
        logger.info("Check-in deleted: %s", checkin.id)
