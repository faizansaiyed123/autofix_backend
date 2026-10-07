"""API routes for digital vehicle inspections.

Endpoints:
- GET    /                       List inspections (paginated, filterable)
- POST   /                       Create an inspection with optional items
- GET    /{id}                   Get an inspection
- PATCH  /{id}                   Update an inspection
- PATCH  /{id}/status            Transition inspection status
- POST   /{id}/items             Add an inspection item
- PATCH  /{id}/items/{item_id}   Update an inspection item
- DELETE /{id}/items/{item_id}   Remove an inspection item
- GET    /{id}/report            Customer-facing visual inspection report
- DELETE /{id}                   Delete an inspection
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.auth.dependencies import require_permission
from app.auth.models import User
from app.auth.permissions import PermissionEnum
from app.common.schemas import PaginatedResponse
from app.core.database import AsyncSession, get_session
from app.inspections.schemas import (
    InspectionCreate,
    InspectionItemCreate,
    InspectionItemRead,
    InspectionItemUpdate,
    InspectionRead,
    InspectionReport,
    InspectionUpdate,
)
from app.inspections.services import InspectionService

router = APIRouter()


@router.get("/", response_model=PaginatedResponse[InspectionRead])
async def list_inspections(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    vehicle_id: UUID | None = Query(None),
    customer_id: UUID | None = Query(None),
    technician_id: UUID | None = Query(None),
    current_user: User = Depends(require_permission(PermissionEnum.INSPECTIONS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """List inspections with pagination and filtering."""
    service = InspectionService(session)
    inspections, total = await service.list_inspections(
        page=page,
        size=size,
        status=status,
        vehicle_id=vehicle_id,
        customer_id=customer_id,
        technician_id=technician_id,
    )
    return PaginatedResponse[InspectionRead].create(
        items=[InspectionRead.model_validate(i) for i in inspections],
        page=page,
        size=size,
        total=total,
    )


@router.post("/", response_model=InspectionRead, status_code=status.HTTP_201_CREATED)
async def create_inspection(
    inspection_data: InspectionCreate,
    current_user: User = Depends(require_permission(PermissionEnum.INSPECTIONS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Create a new inspection with optional items and photos."""
    service = InspectionService(session)
    inspection = await service.create_inspection(inspection_data)
    return InspectionRead.model_validate(inspection)


@router.get("/{inspection_id}", response_model=InspectionRead)
async def get_inspection(
    inspection_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.INSPECTIONS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get an inspection by ID."""
    service = InspectionService(session)
    return InspectionRead.model_validate(await service.get_by_id(inspection_id))


@router.patch("/{inspection_id}", response_model=InspectionRead)
async def update_inspection(
    inspection_id: UUID,
    inspection_data: InspectionUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.INSPECTIONS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Update an inspection's status, technician, mileage or notes."""
    service = InspectionService(session)
    inspection = await service.update_inspection(inspection_id, inspection_data)
    return InspectionRead.model_validate(inspection)


@router.patch("/{inspection_id}/status", response_model=InspectionRead)
async def update_inspection_status(
    inspection_id: UUID,
    status: Annotated[str, Query(description="DRAFT, IN_PROGRESS, COMPLETED or CANCELLED")],
    current_user: User = Depends(require_permission(PermissionEnum.INSPECTIONS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Transition an inspection to a new status."""
    service = InspectionService(session)
    inspection = await service.update_status(inspection_id, status)
    return InspectionRead.model_validate(inspection)


@router.post(
    "/{inspection_id}/items",
    response_model=InspectionRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_inspection_item(
    inspection_id: UUID,
    item_data: InspectionItemCreate,
    current_user: User = Depends(require_permission(PermissionEnum.INSPECTIONS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Add an inspection item to an existing inspection."""
    service = InspectionService(session)
    await service.add_item(inspection_id, item_data)
    return InspectionRead.model_validate(await service.get_by_id(inspection_id))


@router.patch("/{inspection_id}/items/{item_id}", response_model=InspectionItemRead)
async def update_inspection_item(
    inspection_id: UUID,
    item_id: UUID,
    item_data: InspectionItemUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.INSPECTIONS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Update a single inspection item."""
    service = InspectionService(session)
    item = await service.update_item(inspection_id, item_id, item_data)
    return InspectionItemRead.model_validate(item)


@router.delete(
    "/{inspection_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_inspection_item(
    inspection_id: UUID,
    item_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.INSPECTIONS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Remove an inspection item."""
    service = InspectionService(session)
    await service.delete_item(inspection_id, item_id)


@router.get("/{inspection_id}/report", response_model=InspectionReport)
async def get_inspection_report(
    inspection_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.INSPECTIONS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Generate the customer-facing visual inspection report."""
    service = InspectionService(session)
    return await service.generate_report(inspection_id)


@router.delete("/{inspection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_inspection(
    inspection_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.INSPECTIONS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Delete an inspection."""
    service = InspectionService(session)
    await service.delete_inspection(inspection_id)
