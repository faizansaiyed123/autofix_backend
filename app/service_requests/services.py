"""Service request management business logic.

Handles service request CRUD operations, status transitions,
and business rules.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import BusinessRuleError, NotFoundError
from app.service_requests.models import ServiceRequest, ServiceRequestStatus
from app.service_requests.schemas import ServiceRequestCreate, ServiceRequestUpdate

logger = logging.getLogger("autofix.service_requests.services")

VALID_TRANSITIONS: dict[str, list[str]] = {
    ServiceRequestStatus.NEW.value: [ServiceRequestStatus.IN_REVIEW.value, ServiceRequestStatus.REJECTED.value],
    ServiceRequestStatus.IN_REVIEW.value: [ServiceRequestStatus.APPROVED.value, ServiceRequestStatus.REJECTED.value],
    ServiceRequestStatus.APPROVED.value: [ServiceRequestStatus.CONVERTED.value, ServiceRequestStatus.REJECTED.value],
    ServiceRequestStatus.REJECTED.value: [],
    ServiceRequestStatus.CONVERTED.value: [],
}


def _validate_status_transition(current: str, new: str) -> None:
    """Validate that a status transition is allowed."""
    allowed = VALID_TRANSITIONS.get(current, [])
    if new not in allowed:
        raise BusinessRuleError(
            f"Cannot transition status from '{current}' to '{new}'. Allowed: {allowed}"
        )


async def validate_customer_exists(db: AsyncSession, customer_id: UUID | str) -> bool:
    """Check that a customer exists."""
    from app.customers.models import Customer
    result = await db.execute(select(Customer).where(Customer.id == str(customer_id)))
    return result.scalar_one_or_none() is not None


async def validate_vehicle_exists(db: AsyncSession, vehicle_id: UUID | str) -> bool:
    """Check that a vehicle exists."""
    from app.vehicles.models import Vehicle
    result = await db.execute(select(Vehicle).where(Vehicle.id == str(vehicle_id)))
    return result.scalar_one_or_none() is not None


class ServiceRequestService:
    """Service for service request management operations."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, request_id: UUID | str) -> ServiceRequest:
        """Get a service request by ID."""
        result = await self.db.execute(select(ServiceRequest).where(ServiceRequest.id == str(request_id)))
        sr = result.scalar_one_or_none()
        if not sr:
            raise NotFoundError(f"Service request with id {request_id} not found")
        return sr

    async def list_requests(
        self,
        *,
        page: int = 1,
        size: int = 20,
        status: str | None = None,
        priority: str | None = None,
        customer_id: UUID | str | None = None,
    ) -> tuple[list[ServiceRequest], int]:
        """List service requests with filtering and pagination."""
        stmt = select(ServiceRequest)
        count_stmt = select(func.count()).select_from(ServiceRequest)

        if status:
            stmt = stmt.where(ServiceRequest.status == status)
            count_stmt = count_stmt.where(ServiceRequest.status == status)

        if priority:
            stmt = stmt.where(ServiceRequest.priority == priority)
            count_stmt = count_stmt.where(ServiceRequest.priority == priority)

        if customer_id:
            stmt = stmt.where(ServiceRequest.customer_id == str(customer_id))
            count_stmt = count_stmt.where(ServiceRequest.customer_id == str(customer_id))

        count_result = await self.db.execute(count_stmt)
        total = count_result.scalar_one()

        offset = (page - 1) * size
        stmt = stmt.order_by(ServiceRequest.created_at.desc()).offset(offset).limit(size)
        result = await self.db.execute(stmt)
        requests = result.scalars().all()

        return requests, total

    async def create_request(self, request_data: ServiceRequestCreate) -> ServiceRequest:
        """Create a new service request."""
        if not await validate_customer_exists(self.db, request_data.customer_id):
            from app.common.exceptions import ConflictError
            raise ConflictError(f"Customer with id {request_data.customer_id} not found")

        if request_data.vehicle_id and not await validate_vehicle_exists(self.db, request_data.vehicle_id):
            from app.common.exceptions import ConflictError
            raise ConflictError(f"Vehicle with id {request_data.vehicle_id} not found")

        sr = ServiceRequest(
            customer_id=str(request_data.customer_id),
            vehicle_id=str(request_data.vehicle_id) if request_data.vehicle_id else None,
            title=request_data.title,
            description=request_data.description,
            priority=request_data.priority,
            service_advisor_notes=request_data.service_advisor_notes,
        )

        self.db.add(sr)
        await self.db.commit()
        await self.db.refresh(sr)
        logger.info("Service request created: %s", sr.id)
        return sr

    async def update_request(self, request_id: UUID | str, request_data: ServiceRequestUpdate) -> ServiceRequest:
        """Update an existing service request."""
        sr = await self.get_by_id(request_id)

        update_data = request_data.model_dump(exclude_unset=True)

        # Validate status transition
        if "status" in update_data and update_data["status"] is not None:
            _validate_status_transition(sr.status, update_data["status"])

        for field, value in update_data.items():
            if value is not None:
                setattr(sr, field, value)

        await self.db.commit()
        await self.db.refresh(sr)
        logger.info("Service request updated: %s", sr.id)
        return sr

    async def update_status(self, request_id: UUID | str, status: str) -> ServiceRequest:
        """Update service request status with transition validation."""
        try:
            ServiceRequestStatus(status)
        except ValueError:
            raise BusinessRuleError(f"Invalid service request status: {status}")

        sr = await self.get_by_id(request_id)
        _validate_status_transition(sr.status, status)
        sr.status = status
        await self.db.commit()
        await self.db.refresh(sr)
        logger.info("Service request %s status -> %s", sr.id, status)
        return sr

    async def delete_request(self, request_id: UUID | str) -> None:
        """Delete a service request."""
        sr = await self.get_by_id(request_id)
        await self.db.delete(sr)
        await self.db.commit()
        logger.info("Service request deleted: %s", sr.id)
