"""API routes for the technician-to-parts-staff request workflow.

Endpoints:
- GET    /                     List part requests (paginated, filterable)
- POST   /                     Raise a part request
- GET    /{id}                 Get a part request
- PATCH  /{id}                 Edit a pending request
- POST   /{id}/decision        Approve or reject (parts staff)
- POST   /{id}/fulfil          Mark an approved request as staged
- POST   /{id}/cancel          Withdraw a request
- DELETE /{id}                 Delete a pending request
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.auth.dependencies import require_permission
from app.auth.models import User
from app.auth.permissions import PermissionEnum
from app.common.schemas import PaginatedResponse
from app.core.database import AsyncSession, get_session
from app.part_requests.schemas import (
    PartRequestCreate,
    PartRequestDecision,
    PartRequestRead,
    PartRequestUpdate,
)
from app.part_requests.services import PartRequestService

router = APIRouter()


@router.get("/", response_model=PaginatedResponse[PartRequestRead])
async def list_part_requests(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    repair_order_id: UUID | None = Query(None),
    requested_by_id: UUID | None = Query(None),
    current_user: User = Depends(require_permission(PermissionEnum.PART_REQUESTS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """List part requests with pagination and filtering."""
    service = PartRequestService(session)
    requests, total = await service.list_part_requests(
        page=page,
        size=size,
        status=status,
        repair_order_id=repair_order_id,
        requested_by_id=requested_by_id,
    )
    return PaginatedResponse[PartRequestRead].create(
        items=[PartRequestRead.model_validate(r) for r in requests],
        page=page,
        size=size,
        total=total,
    )


@router.post("/", response_model=PartRequestRead, status_code=status.HTTP_201_CREATED)
async def create_part_request(
    request_data: PartRequestCreate,
    current_user: User = Depends(require_permission(PermissionEnum.PART_REQUESTS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Raise a part request against a repair order."""
    service = PartRequestService(session)
    request = await service.create_part_request(request_data, requested_by_id=current_user.id)
    return PartRequestRead.model_validate(request)


@router.get("/{request_id}", response_model=PartRequestRead)
async def get_part_request(
    request_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.PART_REQUESTS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get a part request by ID."""
    service = PartRequestService(session)
    return PartRequestRead.model_validate(await service.get_by_id(request_id))


@router.patch("/{request_id}", response_model=PartRequestRead)
async def update_part_request(
    request_id: UUID,
    request_data: PartRequestUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.PART_REQUESTS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Edit a pending part request."""
    service = PartRequestService(session)
    request = await service.update_part_request(request_id, request_data)
    return PartRequestRead.model_validate(request)


@router.post("/{request_id}/decision", response_model=PartRequestRead)
async def decide_part_request(
    request_id: UUID,
    decision: PartRequestDecision,
    current_user: User = Depends(require_permission(PermissionEnum.PART_REQUEST_APPROVE)),
    session: AsyncSession = Depends(get_session),
):
    """Approve or reject a part request.

    Gated on ``part_requests:approve``, which parts staff and the owner hold —
    the technician who raised the request cannot decide it.
    """
    service = PartRequestService(session)
    request = await service.decide_part_request(
        request_id,
        decision.decision,
        decided_by_id=current_user.id,
        decision_reason=decision.decision_reason,
    )
    return PartRequestRead.model_validate(request)


@router.post("/{request_id}/fulfil", response_model=PartRequestRead)
async def fulfil_part_request(
    request_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.PART_REQUEST_APPROVE)),
    session: AsyncSession = Depends(get_session),
):
    """Mark an approved part request as staged for the job."""
    service = PartRequestService(session)
    request = await service.fulfil_part_request(request_id, decided_by_id=current_user.id)
    return PartRequestRead.model_validate(request)


@router.post("/{request_id}/cancel", response_model=PartRequestRead)
async def cancel_part_request(
    request_id: UUID,
    reason: str | None = Query(None, max_length=1000),
    current_user: User = Depends(require_permission(PermissionEnum.PART_REQUESTS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Withdraw a part request."""
    service = PartRequestService(session)
    request = await service.cancel_part_request(request_id, reason)
    return PartRequestRead.model_validate(request)


@router.delete("/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_part_request(
    request_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.PART_REQUESTS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Delete a pending part request."""
    service = PartRequestService(session)
    await service.delete_part_request(request_id)
