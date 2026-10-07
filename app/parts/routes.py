"""API routes for the parts catalog.

Endpoints:
- GET    /                     List parts (paginated, filterable, searchable)
- POST   /                     Add a catalog line
- GET    /categories           Categories in use
- GET    /low-stock            Parts at or below their reorder level
- GET    /{part_id}            Get a part
- PATCH  /{part_id}            Edit details, pricing, reorder level or status
- DELETE /{part_id}            Delete a retired, empty, never-used line

Stock levels are not edited here — they move through
:mod:`app.inventory.routes`.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.auth.dependencies import require_permission
from app.auth.models import User
from app.auth.permissions import PermissionEnum
from app.common.schemas import PaginatedResponse
from app.core.database import AsyncSession, get_session
from app.inventory.services import InventoryService
from app.parts.schemas import PartCreate, PartRead, PartUpdate
from app.parts.services import PartService

router = APIRouter()


@router.get("/", response_model=PaginatedResponse[PartRead])
async def list_parts(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    category: str | None = Query(None),
    status: str | None = Query(None),
    search: str | None = Query(None, max_length=200),
    low_stock: bool | None = Query(None),
    in_stock: bool | None = Query(None),
    current_user: User = Depends(require_permission(PermissionEnum.PARTS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """List the parts catalog with pagination, filtering and search."""
    service = PartService(session)
    parts, total = await service.list_parts(
        page=page,
        size=size,
        category=category,
        status=status,
        search=search,
        low_stock=low_stock,
        in_stock=in_stock,
    )
    return PaginatedResponse[PartRead].create(
        items=[PartRead.model_validate(p) for p in parts],
        page=page,
        size=size,
        total=total,
    )


@router.post("/", response_model=PartRead, status_code=status.HTTP_201_CREATED)
async def create_part(
    part_data: PartCreate,
    current_user: User = Depends(require_permission(PermissionEnum.PARTS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Add a catalog line. The part starts with no stock."""
    service = PartService(session)
    return PartRead.model_validate(await service.create_part(part_data))


@router.get("/categories", response_model=list[str])
async def list_categories(
    current_user: User = Depends(require_permission(PermissionEnum.PARTS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Every category currently in use, for filter dropdowns."""
    service = PartService(session)
    return await service.list_categories()


@router.get("/low-stock", response_model=list)
async def list_low_stock(
    include_discontinued: bool = Query(False),
    current_user: User = Depends(require_permission(PermissionEnum.PARTS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Parts at or below their reorder level, most urgent first."""
    service = InventoryService(session)
    return await service.low_stock_parts(include_discontinued=include_discontinued)


@router.get("/{part_id}", response_model=PartRead)
async def get_part(
    part_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.PARTS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get a catalog line by ID."""
    service = PartService(session)
    return PartRead.model_validate(await service.get_by_id(part_id))


@router.patch("/{part_id}", response_model=PartRead)
async def update_part(
    part_id: UUID,
    part_data: PartUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.PARTS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Edit a part's details, pricing, reorder level or status."""
    service = PartService(session)
    return PartRead.model_validate(await service.update_part(part_id, part_data))


@router.delete("/{part_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_part(
    part_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.PARTS_MANAGE)),
    session: AsyncSession = Depends(get_session),
):
    """Delete a part that has been retired, emptied and never used."""
    service = PartService(session)
    await service.delete_part(part_id)
