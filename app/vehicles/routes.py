"""API routes for vehicle management.

Endpoints:
- GET / - List vehicles (paginated)
- POST / - Create a vehicle
- GET /search - Search vehicles
- GET /{id} - Get a vehicle by ID
- PATCH /{id} - Update a vehicle
- DELETE /{id} - Delete a vehicle
- GET /{id}/mileage - Get mileage history
- POST /{id}/mileage - Add a mileage record
- PATCH /{id}/status - Update vehicle status
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.audit.decorator import audit
from app.audit.models import AuditAction
from app.auth.dependencies import require_permission
from app.auth.models import User
from app.auth.permissions import PermissionEnum
from app.common.schemas import PaginatedResponse, PaginationMeta
from app.core.database import AsyncSession, get_session
from app.vehicles.schemas import MileageRecordCreate, MileageRecordRead, VehicleCreate, VehicleRead, VehicleUpdate
from app.vehicles.services import VehicleService

router = APIRouter()


@router.get("/", response_model=PaginatedResponse[VehicleRead])
async def list_vehicles(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    customer_id: UUID | None = Query(None, description="Filter by customer ID"),
    search: str | None = Query(None),
    current_user: User = Depends(require_permission(PermissionEnum.VEHICLES_READ)),
    session: AsyncSession = Depends(get_session),
):
    """List vehicles with pagination and optional filtering."""
    service = VehicleService(session)
    vehicles, total = await service.list_vehicles(
        page=page, size=size, customer_id=customer_id, search=search
    )

    vehicle_list = [VehicleRead.model_validate(v) for v in vehicles]

    pages = (total + size - 1) // size if size > 0 else 0
    return PaginatedResponse[VehicleRead](
        data=vehicle_list,
        meta=PaginationMeta(page=page, size=size, total=total, pages=pages),
    )


@router.post("/", response_model=VehicleRead, status_code=status.HTTP_201_CREATED)
@audit(AuditAction.CREATE, "vehicle")
async def create_vehicle(
    vehicle_data: VehicleCreate,
    current_user: User = Depends(require_permission(PermissionEnum.VEHICLES_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Create a new vehicle."""
    service = VehicleService(session)
    vehicle = await service.create_vehicle(vehicle_data)
    return VehicleRead.model_validate(vehicle)


@router.get("/search", response_model=list[VehicleRead])
async def search_vehicles(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(require_permission(PermissionEnum.VEHICLES_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Search vehicles by make, model, license plate, or VIN."""
    service = VehicleService(session)
    results = await service.search(q, limit=limit)
    return [VehicleRead.model_validate(v) for v in results]


@router.get("/{vehicle_id}", response_model=VehicleRead)
async def get_vehicle(
    vehicle_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.VEHICLES_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get a vehicle by ID."""
    service = VehicleService(session)
    vehicle = await service.get_by_id(vehicle_id)
    return VehicleRead.model_validate(vehicle)


@router.patch("/{vehicle_id}", response_model=VehicleRead)
@audit(AuditAction.UPDATE, "vehicle")
async def update_vehicle(
    vehicle_id: UUID,
    vehicle_data: VehicleUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.VEHICLES_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Update a vehicle."""
    service = VehicleService(session)
    vehicle = await service.update_vehicle(vehicle_id, vehicle_data)
    return VehicleRead.model_validate(vehicle)


@router.delete("/{vehicle_id}", status_code=status.HTTP_204_NO_CONTENT)
@audit(AuditAction.DELETE, "vehicle")
async def delete_vehicle(
    vehicle_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.VEHICLES_MANAGE)),
    session: AsyncSession = Depends(get_session),
):
    """Delete a vehicle."""
    service = VehicleService(session)
    await service.delete_vehicle(vehicle_id)


@router.get("/{vehicle_id}/mileage", response_model=list[MileageRecordRead])
async def get_mileage_history(
    vehicle_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.VEHICLES_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get mileage history for a vehicle."""
    service = VehicleService(session)
    records = await service.get_mileage_history(vehicle_id)
    return [MileageRecordRead.model_validate(r) for r in records]


@router.post("/{vehicle_id}/mileage", response_model=MileageRecordRead, status_code=status.HTTP_201_CREATED)
async def add_mileage_record(
    vehicle_id: UUID,
    mileage_data: MileageRecordCreate,
    current_user: User = Depends(require_permission(PermissionEnum.VEHICLES_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Add a mileage record for a vehicle."""
    service = VehicleService(session)
    record = await service.add_mileage_record(
        vehicle_id, mileage=mileage_data.mileage, source=mileage_data.source, notes=mileage_data.notes
    )
    return MileageRecordRead.model_validate(record)


@router.patch("/{vehicle_id}/status", response_model=VehicleRead)
@audit(AuditAction.STATUS_CHANGE, "vehicle")
async def update_vehicle_status(
    vehicle_id: UUID,
    status: str,
    current_user: User = Depends(require_permission(PermissionEnum.VEHICLES_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Update vehicle status (ACTIVE, IN_SHOP, ARCHIVED)."""
    service = VehicleService(session)
    vehicle = await service.set_status(vehicle_id, status)
    return VehicleRead.model_validate(vehicle)
