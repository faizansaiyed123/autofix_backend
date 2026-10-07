"""API routes for quality control.

Endpoints:
- GET    /                        List QC attempts (paginated, filterable)
- POST   /                        Start a QC attempt on a completed repair order
- GET    /queue                   Completed repair orders awaiting QC
- GET    /{id}                    Get a QC attempt with its checks and photos
- PATCH  /{id}                    Update the notes on an open attempt
- PATCH  /{id}/checks/{check_type}  Inspector override of one verification
- POST   /{id}/reverify           Re-run the automatic verification checks
- POST   /{id}/photos             Attach a photo
- DELETE /{id}/photos/{photo_id}  Remove a photo
- POST   /{id}/pass               Pass QC and release the repair order
- POST   /{id}/fail               Fail QC and send the order back for rework
- DELETE /{id}                    Discard an attempt raised in error
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.auth.dependencies import require_permission
from app.auth.models import User
from app.auth.permissions import PermissionEnum
from app.common.schemas import PaginatedResponse
from app.core.database import AsyncSession, get_session
from app.qc.schemas import (
    QCCheckItemOverride,
    QCPhotoCreate,
    QCQueueItem,
    QualityCheckCreate,
    QualityCheckDecision,
    QualityCheckRead,
    QualityCheckUpdate,
)
from app.qc.services import QualityControlService

router = APIRouter()


@router.get("/", response_model=PaginatedResponse[QualityCheckRead])
async def list_quality_checks(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    repair_order_id: UUID | None = Query(None),
    inspector_id: UUID | None = Query(None),
    current_user: User = Depends(require_permission(PermissionEnum.QC_READ)),
    session: AsyncSession = Depends(get_session),
):
    """List quality control attempts."""
    service = QualityControlService(session)
    checks, total = await service.list_checks(
        page=page,
        size=size,
        status=status,
        repair_order_id=repair_order_id,
        inspector_id=inspector_id,
    )
    return PaginatedResponse[QualityCheckRead].create(
        items=[QualityCheckRead.model_validate(c) for c in checks],
        page=page,
        size=size,
        total=total,
    )


@router.get("/queue", response_model=list[QCQueueItem])
async def get_qc_queue(
    current_user: User = Depends(require_permission(PermissionEnum.QC_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Completed repair orders still waiting on quality control."""
    service = QualityControlService(session)
    return await service.get_queue()


@router.post("/", response_model=QualityCheckRead, status_code=status.HTTP_201_CREATED)
async def create_quality_check(
    check_data: QualityCheckCreate,
    current_user: User = Depends(require_permission(PermissionEnum.QC_PERFORM)),
    session: AsyncSession = Depends(get_session),
):
    """Start a QC attempt against a completed repair order."""
    service = QualityControlService(session)
    check = await service.create_check(check_data, inspector_id=current_user.id)
    return QualityCheckRead.model_validate(check)


@router.get("/{quality_check_id}", response_model=QualityCheckRead)
async def get_quality_check(
    quality_check_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.QC_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get a quality control attempt with its verification checks."""
    service = QualityControlService(session)
    return QualityCheckRead.model_validate(await service.get_by_id(quality_check_id))


@router.patch("/{quality_check_id}", response_model=QualityCheckRead)
async def update_quality_check(
    quality_check_id: UUID,
    check_data: QualityCheckUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.QC_PERFORM)),
    session: AsyncSession = Depends(get_session),
):
    """Update the notes on an open quality control attempt."""
    service = QualityControlService(session)
    check = await service.update_check(quality_check_id, check_data)
    return QualityCheckRead.model_validate(check)


@router.patch("/{quality_check_id}/checks/{check_type}", response_model=QualityCheckRead)
async def override_qc_check(
    quality_check_id: UUID,
    check_type: str,
    override: QCCheckItemOverride,
    current_user: User = Depends(require_permission(PermissionEnum.QC_PERFORM)),
    session: AsyncSession = Depends(get_session),
):
    """Record an inspector's manual verdict on one verification check."""
    service = QualityControlService(session)
    check = await service.override_check(quality_check_id, check_type, override)
    return QualityCheckRead.model_validate(check)


@router.post("/{quality_check_id}/reverify", response_model=QualityCheckRead)
async def reverify_quality_check(
    quality_check_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.QC_PERFORM)),
    session: AsyncSession = Depends(get_session),
):
    """Re-run the automatic verification checks against current shop data."""
    service = QualityControlService(session)
    check = await service.reverify(quality_check_id)
    return QualityCheckRead.model_validate(check)


@router.post("/{quality_check_id}/photos", response_model=QualityCheckRead)
async def add_qc_photo(
    quality_check_id: UUID,
    photo_data: QCPhotoCreate,
    current_user: User = Depends(require_permission(PermissionEnum.QC_PERFORM)),
    session: AsyncSession = Depends(get_session),
):
    """Attach a photo documenting the finished work."""
    service = QualityControlService(session)
    check = await service.add_photo(quality_check_id, photo_data)
    return QualityCheckRead.model_validate(check)


@router.delete("/{quality_check_id}/photos/{photo_id}", response_model=QualityCheckRead)
async def delete_qc_photo(
    quality_check_id: UUID,
    photo_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.QC_PERFORM)),
    session: AsyncSession = Depends(get_session),
):
    """Remove a photo from an open quality control attempt."""
    service = QualityControlService(session)
    check = await service.delete_photo(quality_check_id, photo_id)
    return QualityCheckRead.model_validate(check)


@router.post("/{quality_check_id}/pass", response_model=QualityCheckRead)
async def pass_quality_check(
    quality_check_id: UUID,
    decision: QualityCheckDecision | None = None,
    current_user: User = Depends(require_permission(PermissionEnum.QC_PERFORM)),
    session: AsyncSession = Depends(get_session),
):
    """Pass quality control and release the repair order for delivery."""
    service = QualityControlService(session)
    check = await service.pass_check(
        quality_check_id, decision.notes if decision else None
    )
    return QualityCheckRead.model_validate(check)


@router.post("/{quality_check_id}/fail", response_model=QualityCheckRead)
async def fail_quality_check(
    quality_check_id: UUID,
    decision: QualityCheckDecision,
    current_user: User = Depends(require_permission(PermissionEnum.QC_PERFORM)),
    session: AsyncSession = Depends(get_session),
):
    """Fail quality control and send the repair order back for rework.

    A reason is mandatory: a rework loop with no explanation is how the same
    defect ships twice.
    """
    service = QualityControlService(session)
    check = await service.fail_check(quality_check_id, decision.reason or "")
    return QualityCheckRead.model_validate(check)


@router.delete("/{quality_check_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_quality_check(
    quality_check_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.QC_PERFORM)),
    session: AsyncSession = Depends(get_session),
):
    """Discard an in-progress QC attempt that was raised in error."""
    service = QualityControlService(session)
    await service.delete_check(quality_check_id)
