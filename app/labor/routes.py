"""API routes for labor tracking and the technician dashboard.

Endpoints:
- GET    /                     List labor records (paginated, filterable)
- POST   /                     Log labor against a repair order
- GET    /{id}                 Get a labor record
- PATCH  /{id}                 Update a labor record
- DELETE /{id}                 Delete a labor record
- GET    /dashboard            Aggregate workload for a technician
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.auth.dependencies import require_permission
from app.auth.models import User
from app.auth.permissions import PermissionEnum
from app.common.schemas import PaginatedResponse
from app.core.database import AsyncSession, get_session
from app.labor.schemas import (
    LaborRecordCreate,
    LaborRecordRead,
    LaborRecordUpdate,
    TechnicianDashboard,
)
from app.labor.services import LaborService

router = APIRouter()


@router.get("/dashboard", response_model=TechnicianDashboard)
async def get_dashboard(
    technician_id: UUID = Query(..., description="Technician to summarize"),
    current_user: User = Depends(require_permission(PermissionEnum.LABOR_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Aggregate a technician's open work, hours, and pending part requests."""
    service = LaborService(session)
    return await service.get_dashboard(technician_id)


@router.get("/", response_model=PaginatedResponse[LaborRecordRead])
async def list_labor_records(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    repair_order_id: UUID | None = Query(None),
    repair_task_id: UUID | None = Query(None),
    technician_id: UUID | None = Query(None),
    current_user: User = Depends(require_permission(PermissionEnum.LABOR_READ)),
    session: AsyncSession = Depends(get_session),
):
    """List labor records with pagination and filtering."""
    service = LaborService(session)
    records, total = await service.list_labor_records(
        page=page,
        size=size,
        repair_order_id=repair_order_id,
        repair_task_id=repair_task_id,
        technician_id=technician_id,
    )
    return PaginatedResponse[LaborRecordRead].create(
        items=[LaborRecordRead.model_validate(r) for r in records],
        page=page,
        size=size,
        total=total,
    )


@router.post("/", response_model=LaborRecordRead, status_code=status.HTTP_201_CREATED)
async def create_labor_record(
    labor_data: LaborRecordCreate,
    current_user: User = Depends(require_permission(PermissionEnum.LABOR_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Log technician time against a repair order."""
    service = LaborService(session)
    record = await service.create_labor_record(labor_data)
    return LaborRecordRead.model_validate(record)


@router.get("/{labor_id}", response_model=LaborRecordRead)
async def get_labor_record(
    labor_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.LABOR_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get a labor record by ID."""
    service = LaborService(session)
    return LaborRecordRead.model_validate(await service.get_by_id(labor_id))


@router.patch("/{labor_id}", response_model=LaborRecordRead)
async def update_labor_record(
    labor_id: UUID,
    labor_data: LaborRecordUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.LABOR_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Update a labor record while its repair order is still in progress."""
    service = LaborService(session)
    record = await service.update_labor_record(labor_id, labor_data)
    return LaborRecordRead.model_validate(record)


@router.delete("/{labor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_labor_record(
    labor_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.LABOR_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Delete a labor record while its repair order is still in progress."""
    service = LaborService(session)
    await service.delete_labor_record(labor_id)
