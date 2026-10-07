"""API routes for purchase orders and goods receiving.

Endpoints:
- GET    /                     List purchase orders (paginated, filterable)
- POST   /                     Raise an order with its lines
- POST   /from-low-stock       Raise a draft order from the low-stock list
- GET    /summary              Headline numbers across the order book
- GET    /by-number/{po_number}  Look an order up by its number
- GET    /{po_id}              Get an order with its lines
- PATCH  /{po_id}              Edit a draft order (replaces its lines)
- POST   /{po_id}/items        Add a line to a draft order
- PATCH  /{po_id}/items/{item_id}  Edit one line
- DELETE /{po_id}/items/{item_id}  Drop one line
- POST   /{po_id}/send         Send a draft to the supplier
- POST   /{po_id}/cancel       Call the order off
- PATCH  /{po_id}/status       Generic status transition
- POST   /{po_id}/receive      Book a delivery in (files inventory receipts)
- DELETE /{po_id}              Delete a draft order

``/summary`` and ``/by-number`` are declared before ``/{po_id}`` on purpose: the
id route would otherwise swallow them and reject a non-UUID as a 422.

Receiving requires ``purchase_orders:write``, which parts staff and the owner
hold — the same department that may move stock holds the purchase order.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.auth.dependencies import require_permission
from app.auth.models import User
from app.auth.permissions import PermissionEnum
from app.common.schemas import PaginatedResponse
from app.core.database import AsyncSession, get_session
from app.purchase_orders.schemas import (
    CancelRequest,
    PurchaseOrderCreate,
    PurchaseOrderFromLowStock,
    PurchaseOrderItemCreate,
    PurchaseOrderItemRead,
    PurchaseOrderItemUpdate,
    PurchaseOrderRead,
    PurchaseOrderSummary,
    PurchaseOrderUpdate,
    ReceiveRequest,
    ReceiveResult,
    StatusChangeRequest,
)
from app.purchase_orders.services import PurchaseOrderService

router = APIRouter()


@router.get("/", response_model=PaginatedResponse[PurchaseOrderRead])
async def list_purchase_orders(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    supplier_id: UUID | None = Query(None),
    overdue_only: bool = Query(False),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    current_user: User = Depends(require_permission(PermissionEnum.PURCHASE_ORDERS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """List purchase orders, newest first."""
    service = PurchaseOrderService(session)
    orders, total = await service.list_purchase_orders(
        page=page,
        size=size,
        status=status_filter,
        supplier_id=supplier_id,
        overdue_only=overdue_only,
        start_date=start_date,
        end_date=end_date,
    )
    return PaginatedResponse[PurchaseOrderRead].create(
        items=[PurchaseOrderRead.model_validate(po) for po in orders],
        page=page,
        size=size,
        total=total,
    )


@router.post("/", response_model=PurchaseOrderRead, status_code=status.HTTP_201_CREATED)
async def create_purchase_order(
    order_data: PurchaseOrderCreate,
    current_user: User = Depends(require_permission(PermissionEnum.PURCHASE_ORDERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Raise a purchase order. It starts as a DRAFT, editable until it is sent."""
    service = PurchaseOrderService(session)
    order = await service.create_purchase_order(order_data, created_by_id=current_user.id)
    return PurchaseOrderRead.model_validate(order)


@router.post(
    "/from-low-stock", response_model=PurchaseOrderRead, status_code=status.HTTP_201_CREATED
)
async def create_from_low_stock(
    order_data: PurchaseOrderFromLowStock,
    current_user: User = Depends(require_permission(PermissionEnum.PURCHASE_ORDERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Raise a draft order covering every part at or below its reorder level.

    Quantities are the shortage multiplied by ``shortage_multiplier``, so the
    result is a draft a buyer can edit before it goes out rather than an order
    placed on their behalf.
    """
    service = PurchaseOrderService(session)
    order = await service.create_from_low_stock(order_data, created_by_id=current_user.id)
    return PurchaseOrderRead.model_validate(order)


@router.get("/summary", response_model=PurchaseOrderSummary)
async def purchase_order_summary(
    current_user: User = Depends(require_permission(PermissionEnum.PURCHASE_ORDERS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Headline numbers across the order book: open, overdue, received, committed."""
    service = PurchaseOrderService(session)
    return await service.get_summary()


@router.get("/by-number/{po_number}", response_model=PurchaseOrderRead)
async def get_purchase_order_by_number(
    po_number: str,
    current_user: User = Depends(require_permission(PermissionEnum.PURCHASE_ORDERS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Look an order up by its human-facing number, e.g. from a delivery note."""
    service = PurchaseOrderService(session)
    return PurchaseOrderRead.model_validate(await service.get_by_number(po_number))


@router.get("/{purchase_order_id}", response_model=PurchaseOrderRead)
async def get_purchase_order(
    purchase_order_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.PURCHASE_ORDERS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get a purchase order with its lines and received counts."""
    service = PurchaseOrderService(session)
    return PurchaseOrderRead.model_validate(
        await service.get_by_id(purchase_order_id)
    )


@router.patch("/{purchase_order_id}", response_model=PurchaseOrderRead)
async def update_purchase_order(
    purchase_order_id: UUID,
    order_data: PurchaseOrderUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.PURCHASE_ORDERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Edit a draft order. Supplying ``items`` replaces every line."""
    service = PurchaseOrderService(session)
    order = await service.update_purchase_order(purchase_order_id, order_data)
    return PurchaseOrderRead.model_validate(order)


@router.post(
    "/{purchase_order_id}/items",
    response_model=PurchaseOrderRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_item(
    purchase_order_id: UUID,
    item_data: PurchaseOrderItemCreate,
    current_user: User = Depends(require_permission(PermissionEnum.PURCHASE_ORDERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Append a line to a draft order."""
    service = PurchaseOrderService(session)
    order = await service.add_item(purchase_order_id, item_data)
    return PurchaseOrderRead.model_validate(order)


@router.patch(
    "/{purchase_order_id}/items/{item_id}", response_model=PurchaseOrderItemRead
)
async def update_item(
    purchase_order_id: UUID,
    item_id: UUID,
    item_data: PurchaseOrderItemUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.PURCHASE_ORDERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Edit one line of a draft order."""
    service = PurchaseOrderService(session)
    order = await service.update_item(purchase_order_id, item_id, item_data)
    item = next(i for i in order.items if str(i.id) == str(item_id))
    return PurchaseOrderItemRead.model_validate(item)


@router.delete("/{purchase_order_id}/items/{item_id}", response_model=PurchaseOrderRead)
async def remove_item(
    purchase_order_id: UUID,
    item_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.PURCHASE_ORDERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Drop a line from a draft order and renumber the rest."""
    service = PurchaseOrderService(session)
    return PurchaseOrderRead.model_validate(await service.remove_item(purchase_order_id, item_id))


@router.post("/{purchase_order_id}/send", response_model=PurchaseOrderRead)
async def send_purchase_order(
    purchase_order_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.PURCHASE_ORDERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Send a draft order to the supplier.

    After this the lines are frozen: the supplier may already hold the goods, so
    the order is corrected by cancelling it and raising another.
    """
    service = PurchaseOrderService(session)
    return PurchaseOrderRead.model_validate(await service.send(purchase_order_id))


@router.post("/{purchase_order_id}/cancel", response_model=PurchaseOrderRead)
async def cancel_purchase_order(
    purchase_order_id: UUID,
    payload: CancelRequest | None = None,
    current_user: User = Depends(require_permission(PermissionEnum.PURCHASE_ORDERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Call a purchase order off, optionally recording why."""
    service = PurchaseOrderService(session)
    reason = payload.reason if payload else None
    return PurchaseOrderRead.model_validate(await service.cancel(purchase_order_id, reason))


@router.patch("/{purchase_order_id}/status", response_model=PurchaseOrderRead)
async def update_purchase_order_status(
    purchase_order_id: UUID,
    payload: StatusChangeRequest,
    current_user: User = Depends(require_permission(PermissionEnum.PURCHASE_ORDERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Move an order through the status machine.

    ``SENT`` and ``CANCELLED`` route through the same code as the dedicated
    endpoints, so the guards and the milestone stamps cannot be bypassed here.
    Receiving is deliberately not a status you can set this way: it has to go
    through ``/receive`` so stock is booked in with it.
    """
    service = PurchaseOrderService(session)
    order = await service.update_status(purchase_order_id, payload.status, payload.reason)
    return PurchaseOrderRead.model_validate(order)


@router.post("/{purchase_order_id}/receive", response_model=ReceiveResult)
async def receive_delivery(
    purchase_order_id: UUID,
    receive_data: ReceiveRequest,
    current_user: User = Depends(require_permission(PermissionEnum.PURCHASE_ORDERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Book a delivery in against a sent order.

    Files one inventory ``RECEIPT`` per line, references the PO number, and moves
    the order to ``PARTIALLY_RECEIVED`` or ``RECEIVED``. The whole delivery is
    one transaction: if any line cannot be booked in, no stock moves at all.
    """
    service = PurchaseOrderService(session)
    return await service.receive(purchase_order_id, receive_data, performed_by_id=current_user.id)


@router.delete("/{purchase_order_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_purchase_order(
    purchase_order_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.PURCHASE_ORDERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Delete a draft order. A sent or received order is cancelled, not deleted."""
    service = PurchaseOrderService(session)
    await service.delete_purchase_order(purchase_order_id)
