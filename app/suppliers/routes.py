"""API routes for suppliers.

Endpoints:
- GET    /                     List suppliers (paginated, filterable, searchable)
- POST   /                     Add a supplier
- GET    /active               Active suppliers, for order dropdowns
- GET    /{supplier_id}        Get a supplier
- GET    /{supplier_id}/summary   What the shop has bought from them
- PATCH  /{supplier_id}        Edit details, status or preference
- POST   /{supplier_id}/deactivate  Stop trading with them, keeping history
- DELETE /{supplier_id}        Delete a supplier never ordered from

All of these are gated on the supplier permissions, which parts staff and the
owner hold. A supplier's order book is a purchasing decision, not something a
service advisor or a technician needs to see.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.auth.dependencies import require_permission
from app.auth.models import User
from app.auth.permissions import PermissionEnum
from app.common.schemas import PaginatedResponse
from app.core.database import AsyncSession, get_session
from app.suppliers.models import SupplierStatus
from app.suppliers.schemas import (
    SupplierCreate,
    SupplierRead,
    SupplierSummary,
    SupplierUpdate,
)
from app.suppliers.services import SupplierService

router = APIRouter()


@router.get("/", response_model=PaginatedResponse[SupplierRead])
async def list_suppliers(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    search: str | None = Query(None, max_length=200),
    preferred_only: bool = Query(False),
    with_orders: bool | None = Query(None),
    current_user: User = Depends(require_permission(PermissionEnum.SUPPLIERS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """List suppliers with pagination, filtering and search."""
    service = SupplierService(session)
    suppliers, total = await service.list_suppliers(
        page=page,
        size=size,
        status=status_filter,
        search=search,
        preferred_only=preferred_only,
        with_orders=with_orders,
    )
    return PaginatedResponse[SupplierRead].create(
        items=[SupplierRead.model_validate(s) for s in suppliers],
        page=page,
        size=size,
        total=total,
    )


@router.post("/", response_model=SupplierRead, status_code=status.HTTP_201_CREATED)
async def create_supplier(
    supplier_data: SupplierCreate,
    current_user: User = Depends(require_permission(PermissionEnum.SUPPLIERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Add a supplier to the list."""
    service = SupplierService(session)
    supplier = await service.create_supplier(supplier_data, created_by_id=current_user.id)
    return SupplierRead.model_validate(supplier)


@router.get("/active", response_model=list[SupplierRead])
async def list_active_suppliers(
    preferred_only: bool = Query(False),
    current_user: User = Depends(require_permission(PermissionEnum.SUPPLIERS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Suppliers the shop still buys from, for order dropdowns.

    Unpaginated on purpose: this feeds a picker that has to show everything, and
    the supplier list is a few dozen rows at most.
    """
    service = SupplierService(session)
    suppliers, _ = await service.list_suppliers(
        page=1,
        size=100,
        status=SupplierStatus.ACTIVE.value,
        preferred_only=preferred_only,
    )
    return [SupplierRead.model_validate(s) for s in suppliers]


@router.get("/{supplier_id}", response_model=SupplierRead)
async def get_supplier(
    supplier_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.SUPPLIERS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get a supplier by ID."""
    service = SupplierService(session)
    return SupplierRead.model_validate(await service.get_by_id(supplier_id))


@router.get("/{supplier_id}/summary", response_model=SupplierSummary)
async def supplier_summary(
    supplier_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.SUPPLIERS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Order count, units received and spend for one supplier.

    Spend counts only orders that actually took delivery, so a draft or cancelled
    order never counts against a supplier's record.
    """
    service = SupplierService(session)
    return await service.get_summary(supplier_id)


@router.patch("/{supplier_id}", response_model=SupplierRead)
async def update_supplier(
    supplier_id: UUID,
    supplier_data: SupplierUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.SUPPLIERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Edit a supplier's details, trading status or preference."""
    service = SupplierService(session)
    return SupplierRead.model_validate(await service.update_supplier(supplier_id, supplier_data))


@router.post("/{supplier_id}/deactivate", response_model=SupplierRead)
async def deactivate_supplier(
    supplier_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.SUPPLIERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Stop trading with a supplier.

    Kept as an action rather than a PATCH because it is the one change that has
    a consequence beyond the record: the supplier disappears from order pickers
    and new orders against it are refused.
    """
    service = SupplierService(session)
    return SupplierRead.model_validate(await service.deactivate(supplier_id))


@router.delete("/{supplier_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_supplier(
    supplier_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.SUPPLIERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Delete a supplier that has never been ordered from."""
    service = SupplierService(session)
    await service.delete_supplier(supplier_id)
