"""API routes for check-in management.

Endpoints:
- GET / - List check-ins (paginated)
- POST / - Create a check-in
- GET /{id} - Get a check-in by ID
- PATCH /{id} - Update a check-in
- PATCH /{id}/status - Update check-in status
- DELETE /{id} - Delete a check-in
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.auth.dependencies import require_permission
from app.auth.models import User
from app.auth.permissions import PermissionEnum
from app.checkins.schemas import CheckInCreate, CheckInRead, CheckInUpdate
from app.checkins.services import CheckInService
from app.common.schemas import PaginatedResponse, PaginationMeta
from app.core.database import AsyncSession, get_session

router = APIRouter()


@router.get("/", response_model=PaginatedResponse[CheckInRead])
async def list_checkins(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    vehicle_id: UUID | None = Query(None),
    customer_id: UUID | None = Query(None),
    current_user: User = Depends(require_permission(PermissionEnum.CHECK_INS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """List check-ins with pagination and filtering."""
    service = CheckInService(session)
    checkins, total = await service.list_checkins(
        page=page, size=size, status=status, vehicle_id=vehicle_id, customer_id=customer_id
    )

    checkin_list = [CheckInRead.model_validate(c) for c in checkins]

    pages = (total + size - 1) // size if size > 0 else 0
    return PaginatedResponse[CheckInRead](
        data=checkin_list,
        meta=PaginationMeta(page=page, size=size, total=total, pages=pages),
    )


@router.post("/", response_model=CheckInRead, status_code=status.HTTP_201_CREATED)
async def create_checkin(
    checkin_data: CheckInCreate,
    current_user: User = Depends(require_permission(PermissionEnum.CHECK_INS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Create a new vehicle check-in."""
    service = CheckInService(session)
    checkin = await service.create_checkin(checkin_data)
    return CheckInRead.model_validate(checkin)


@router.get("/{checkin_id}", response_model=CheckInRead)
async def get_checkin(
    checkin_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.CHECK_INS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get a check-in by ID."""
    service = CheckInService(session)
    checkin = await service.get_by_id(checkin_id)
    return CheckInRead.model_validate(checkin)


@router.patch("/{checkin_id}", response_model=CheckInRead)
async def update_checkin(
    checkin_id: UUID,
    checkin_data: CheckInUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.CHECK_INS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Update a check-in."""
    service = CheckInService(session)
    checkin = await service.update_checkin(checkin_id, checkin_data)
    return CheckInRead.model_validate(checkin)


@router.patch("/{checkin_id}/status", response_model=CheckInRead)
async def update_checkin_status(
    checkin_id: UUID,
    status: str,
    current_user: User = Depends(require_permission(PermissionEnum.CHECK_INS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Update check-in status (PENDING, IN_PROGRESS, COMPLETED, CANCELLED)."""
    service = CheckInService(session)
    checkin = await service.update_status(checkin_id, status)
    return CheckInRead.model_validate(checkin)


@router.delete("/{checkin_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_checkin(
    checkin_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.CHECK_INS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Delete a check-in."""
    service = CheckInService(session)
    await service.delete_checkin(checkin_id)
