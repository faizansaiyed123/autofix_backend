"""API routes for the inventory ledger.

Endpoints:
- GET    /transactions        List stock movements (paginated, filterable)
- POST   /transactions        Record a movement and move the balance with it
- GET    /transactions/{id}   Get one movement
- GET    /stock-levels        Balance and valuation for every part
- GET    /stock-summary       Headline numbers across the shelf
- GET    /low-stock           Parts at or below their reorder level
- GET    /parts/{part_id}/history  One part's movements, newest first
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.auth.dependencies import require_permission
from app.auth.models import User
from app.auth.permissions import PermissionEnum
from app.common.schemas import PaginatedResponse
from app.core.database import AsyncSession, get_session
from app.inventory.schemas import (
    InventoryTransactionCreate,
    InventoryTransactionRead,
    LowStockAlert,
    StockLevel,
    StockSummary,
)
from app.inventory.services import InventoryService

router = APIRouter()


@router.get("/transactions", response_model=PaginatedResponse[InventoryTransactionRead])
async def list_transactions(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    part_id: UUID | None = Query(None),
    transaction_type: str | None = Query(None),
    repair_order_id: UUID | None = Query(None),
    performed_by_id: UUID | None = Query(None),
    reference: str | None = Query(None, max_length=100),
    start_date: datetime | None = Query(None),
    end_date: datetime | None = Query(None),
    current_user: User = Depends(require_permission(PermissionEnum.INVENTORY_READ)),
    session: AsyncSession = Depends(get_session),
):
    """List stock movements, newest first."""
    service = InventoryService(session)
    transactions, total = await service.list_transactions(
        page=page,
        size=size,
        part_id=part_id,
        transaction_type=transaction_type,
        repair_order_id=repair_order_id,
        performed_by_id=performed_by_id,
        reference=reference,
        start_date=start_date,
        end_date=end_date,
    )
    return PaginatedResponse[InventoryTransactionRead].create(
        items=[InventoryTransactionRead.model_validate(t) for t in transactions],
        page=page,
        size=size,
        total=total,
    )


@router.post(
    "/transactions",
    response_model=InventoryTransactionRead,
    status_code=status.HTTP_201_CREATED,
)
async def record_transaction(
    transaction_data: InventoryTransactionCreate,
    current_user: User = Depends(require_permission(PermissionEnum.INVENTORY_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Record a stock movement and move the part's balance with it.

    Gated on ``inventory:write``, which parts staff and the owner hold. Anyone
    who can see the catalog can see stock levels, but only the parts department
    moves them.
    """
    service = InventoryService(session)
    transaction = await service.record_transaction(
        transaction_data, performed_by_id=current_user.id
    )
    return InventoryTransactionRead.model_validate(transaction)


@router.get("/transactions/{transaction_id}", response_model=InventoryTransactionRead)
async def get_transaction(
    transaction_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.INVENTORY_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get one recorded stock movement."""
    service = InventoryService(session)
    return InventoryTransactionRead.model_validate(await service.get_transaction(transaction_id))


@router.get("/stock-levels", response_model=list[StockLevel])
async def stock_levels(
    category: str | None = Query(None),
    low_stock_only: bool = Query(False),
    current_user: User = Depends(require_permission(PermissionEnum.INVENTORY_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Balance and valuation for every part."""
    service = InventoryService(session)
    return await service.stock_levels(category=category, low_stock_only=low_stock_only)


@router.get("/stock-summary", response_model=StockSummary)
async def stock_summary(
    current_user: User = Depends(require_permission(PermissionEnum.INVENTORY_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Headline numbers across the shelf: units on hand, value, low stock."""
    service = InventoryService(session)
    return await service.stock_summary()


@router.get("/low-stock", response_model=list[LowStockAlert])
async def low_stock(
    include_discontinued: bool = Query(False),
    current_user: User = Depends(require_permission(PermissionEnum.INVENTORY_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Parts at or below their reorder level, with how far short each one is."""
    service = InventoryService(session)
    return await service.low_stock_parts(include_discontinued=include_discontinued)


@router.get("/parts/{part_id}/history", response_model=list[InventoryTransactionRead])
async def part_history(
    part_id: UUID,
    limit: int = Query(50, ge=1, le=500),
    current_user: User = Depends(require_permission(PermissionEnum.INVENTORY_READ)),
    session: AsyncSession = Depends(get_session),
):
    """One part's stock movements, newest first."""
    service = InventoryService(session)
    transactions = await service.part_history(part_id, limit=limit)
    return [InventoryTransactionRead.model_validate(t) for t in transactions]
