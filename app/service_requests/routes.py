"""API routes for service request management.

Endpoints:
- GET / - List service requests (paginated)
- POST / - Create a service request
- GET /{id} - Get a service request by ID
- PATCH /{id} - Update a service request
- PATCH /{id}/status - Update service request status
- DELETE /{id} - Delete a service request
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.auth.dependencies import require_permission
from app.auth.models import User
from app.auth.permissions import PermissionEnum
from app.common.schemas import PaginatedResponse, PaginationMeta
from app.core.database import AsyncSession, get_session
from app.service_requests.schemas import ServiceRequestCreate, ServiceRequestRead, ServiceRequestUpdate
from app.service_requests.services import ServiceRequestService

router = APIRouter()


@router.get("/", response_model=PaginatedResponse[ServiceRequestRead])
async def list_requests(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    priority: str | None = Query(None),
    customer_id: UUID | None = Query(None),
    current_user: User = Depends(require_permission(PermissionEnum.SERVICE_REQUESTS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """List service requests with pagination and filtering."""
    service = ServiceRequestService(session)
    requests, total = await service.list_requests(
        page=page, size=size, status=status, priority=priority, customer_id=customer_id
    )

    request_list = [ServiceRequestRead.model_validate(r) for r in requests]

    pages = (total + size - 1) // size if size > 0 else 0
    return PaginatedResponse[ServiceRequestRead](
        data=request_list,
        meta=PaginationMeta(page=page, size=size, total=total, pages=pages),
    )


@router.post("/", response_model=ServiceRequestRead, status_code=status.HTTP_201_CREATED)
async def create_request(
    request_data: ServiceRequestCreate,
    current_user: User = Depends(require_permission(PermissionEnum.SERVICE_REQUESTS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Create a new service request."""
    service = ServiceRequestService(session)
    sr = await service.create_request(request_data)
    return ServiceRequestRead.model_validate(sr)


@router.get("/{request_id}", response_model=ServiceRequestRead)
async def get_request(
    request_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.SERVICE_REQUESTS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get a service request by ID."""
    service = ServiceRequestService(session)
    sr = await service.get_by_id(request_id)
    return ServiceRequestRead.model_validate(sr)


@router.patch("/{request_id}", response_model=ServiceRequestRead)
async def update_request(
    request_id: UUID,
    request_data: ServiceRequestUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.SERVICE_REQUESTS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Update a service request."""
    service = ServiceRequestService(session)
    sr = await service.update_request(request_id, request_data)
    return ServiceRequestRead.model_validate(sr)


@router.patch("/{request_id}/status", response_model=ServiceRequestRead)
async def update_request_status(
    request_id: UUID,
    status: str,
    current_user: User = Depends(require_permission(PermissionEnum.SERVICE_REQUESTS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Update service request status (NEW, IN_REVIEW, APPROVED, REJECTED, CONVERTED)."""
    service = ServiceRequestService(session)
    sr = await service.update_status(request_id, status)
    return ServiceRequestRead.model_validate(sr)


@router.delete("/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_request(
    request_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.SERVICE_REQUESTS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Delete a service request."""
    service = ServiceRequestService(session)
    await service.delete_request(request_id)
