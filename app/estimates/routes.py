"""API routes for estimates and customer approval.

Endpoints:
- GET    /                          List estimates (paginated, filterable)
- POST   /                          Create an estimate with optional items
- GET    /{id}                      Get an estimate
- PATCH  /{id}                      Update a draft estimate
- DELETE /{id}                      Delete an estimate
- POST   /{id}/items                Add a priced line
- PATCH  /{id}/items/{item_id}      Update a priced line
- DELETE /{id}/items/{item_id}      Remove a priced line
- PATCH  /{id}/status               Transition estimate status (advisor)
- POST   /{id}/send                 Send the estimate to the customer
- POST   /{id}/cancel               Cancel the estimate
- POST   /{id}/items/{item_id}/decision  Customer approves or declines one line
- GET    /{id}/summary              Customer-facing money summary
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.audit.decorator import audit
from app.audit.models import AuditAction
from app.auth.dependencies import require_permission
from app.auth.models import User
from app.auth.permissions import PermissionEnum
from app.common.schemas import PaginatedResponse
from app.core.database import AsyncSession, get_session
from app.estimates.schemas import (
    EstimateCreate,
    EstimateItemCreate,
    EstimateItemUpdate,
    EstimateRead,
    EstimateSummary,
    EstimateUpdate,
    ItemDecision,
)
from app.estimates.services import EstimateService

router = APIRouter()


@router.get("/", response_model=PaginatedResponse[EstimateRead])
async def list_estimates(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    customer_id: UUID | None = Query(None),
    vehicle_id: UUID | None = Query(None),
    inspection_id: UUID | None = Query(None),
    current_user: User = Depends(require_permission(PermissionEnum.ESTIMATES_READ)),
    session: AsyncSession = Depends(get_session),
):
    """List estimates with pagination and filtering."""
    service = EstimateService(session)
    estimates, total = await service.list_estimates(
        page=page,
        size=size,
        status=status,
        customer_id=customer_id,
        vehicle_id=vehicle_id,
        inspection_id=inspection_id,
    )
    return PaginatedResponse[EstimateRead].create(
        items=[EstimateRead.model_validate(e) for e in estimates],
        page=page,
        size=size,
        total=total,
    )


@router.post("/", response_model=EstimateRead, status_code=status.HTTP_201_CREATED)
@audit(AuditAction.CREATE, "estimate")
async def create_estimate(
    estimate_data: EstimateCreate,
    current_user: User = Depends(require_permission(PermissionEnum.ESTIMATES_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Create an estimate with optional line items."""
    service = EstimateService(session)
    estimate = await service.create_estimate(estimate_data, created_by_id=current_user.id)
    return EstimateRead.model_validate(estimate)


@router.get("/{estimate_id}", response_model=EstimateRead)
async def get_estimate(
    estimate_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.ESTIMATES_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get an estimate with its line items."""
    service = EstimateService(session)
    return EstimateRead.model_validate(await service.get_by_id(estimate_id))


@router.get("/{estimate_id}/summary", response_model=EstimateSummary)
async def get_estimate_summary(
    estimate_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.ESTIMATES_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get the customer-facing money summary for an estimate."""
    service = EstimateService(session)
    return await service.get_summary(estimate_id)


@router.patch("/{estimate_id}", response_model=EstimateRead)
@audit(AuditAction.UPDATE, "estimate")
async def update_estimate(
    estimate_id: UUID,
    estimate_data: EstimateUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.ESTIMATES_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Update a draft estimate's expiry, tax rate or notes."""
    service = EstimateService(session)
    estimate = await service.update_estimate(estimate_id, estimate_data)
    return EstimateRead.model_validate(estimate)


@router.delete("/{estimate_id}", status_code=status.HTTP_204_NO_CONTENT)
@audit(AuditAction.DELETE, "estimate")
async def delete_estimate(
    estimate_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.ESTIMATES_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Delete an estimate the customer has not acted on."""
    service = EstimateService(session)
    await service.delete_estimate(estimate_id)


@router.post(
    "/{estimate_id}/items", response_model=EstimateRead, status_code=status.HTTP_201_CREATED
)
@audit(AuditAction.UPDATE, "estimate", summary="Added a line to an estimate")
async def add_estimate_item(
    estimate_id: UUID,
    item_data: EstimateItemCreate,
    current_user: User = Depends(require_permission(PermissionEnum.ESTIMATES_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Add a priced line to a draft estimate."""
    service = EstimateService(session)
    estimate = await service.add_item(estimate_id, item_data)
    return EstimateRead.model_validate(estimate)


@router.patch("/{estimate_id}/items/{item_id}", response_model=EstimateRead)
@audit(
    AuditAction.UPDATE, "estimate", id_param="estimate_id",
    summary="Corrected a line on an estimate",
)
async def update_estimate_item(
    estimate_id: UUID,
    item_id: UUID,
    item_data: EstimateItemUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.ESTIMATES_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Update a priced line on a draft estimate."""
    service = EstimateService(session)
    estimate = await service.update_item(estimate_id, item_id, item_data)
    return EstimateRead.model_validate(estimate)


@router.delete(
    "/{estimate_id}/items/{item_id}", response_model=EstimateRead
)
@audit(
    AuditAction.UPDATE, "estimate", id_param="estimate_id",
    summary="Removed a line from an estimate",
)
async def delete_estimate_item(
    estimate_id: UUID,
    item_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.ESTIMATES_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Remove a priced line from a draft estimate."""
    service = EstimateService(session)
    estimate = await service.delete_item(estimate_id, item_id)
    return EstimateRead.model_validate(estimate)


@router.patch("/{estimate_id}/status", response_model=EstimateRead)
@audit(AuditAction.STATUS_CHANGE, "estimate")
async def update_estimate_status(
    estimate_id: UUID,
    status: Annotated[
        str,
        Query(description="DRAFT, SENT, APPROVED, PARTIALLY_APPROVED, DECLINED, EXPIRED, CANCELLED"),
    ],
    reason: str | None = Query(None, max_length=1000),
    current_user: User = Depends(require_permission(PermissionEnum.ESTIMATES_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Transition an estimate to a new status."""
    service = EstimateService(session)
    estimate = await service.update_status(estimate_id, status, reason)
    return EstimateRead.model_validate(estimate)


@router.post("/{estimate_id}/send", response_model=EstimateRead)
@audit(AuditAction.SEND, "estimate")
async def send_estimate(
    estimate_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.ESTIMATES_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Send a draft estimate to the customer for approval."""
    service = EstimateService(session)
    estimate = await service.send(estimate_id)
    return EstimateRead.model_validate(estimate)


@router.post("/{estimate_id}/cancel", response_model=EstimateRead)
@audit(AuditAction.STATUS_CHANGE, "estimate", summary="Cancelled an estimate")
async def cancel_estimate(
    estimate_id: UUID,
    reason: str | None = Query(None, max_length=1000),
    current_user: User = Depends(require_permission(PermissionEnum.ESTIMATES_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Cancel an estimate."""
    service = EstimateService(session)
    estimate = await service.cancel(estimate_id, reason)
    return EstimateRead.model_validate(estimate)


@router.post("/{estimate_id}/items/{item_id}/decision", response_model=EstimateRead)
@audit(
    AuditAction.DECIDE, "estimate", id_param="estimate_id",
    summary="Recorded a customer decision on an estimate line",
)
async def decide_estimate_item(
    estimate_id: UUID,
    item_id: UUID,
    decision: ItemDecision,
    current_user: User = Depends(require_permission(PermissionEnum.ESTIMATES_APPROVE)),
    session: AsyncSession = Depends(get_session),
):
    """Record a decision on a single estimate line.

    Gated on ``estimates:approve``, which only the roles that may speak for the
    customer hold: the customer themselves, the owner, and a service advisor
    recording a verbal approval. Technicians and parts staff are locked out.
    """
    service = EstimateService(session)
    estimate = await service.decide_item(estimate_id, item_id, decision)
    return EstimateRead.model_validate(estimate)
