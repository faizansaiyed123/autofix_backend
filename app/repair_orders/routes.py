"""API routes for repair orders and their tasks.

Endpoints:
- GET    /                          List repair orders (paginated, filterable)
- POST   /                          Create a repair order with optional tasks
- GET    /{id}                      Get a repair order
- PATCH  /{id}                      Update the repair order header
- DELETE /{id}                      Delete a repair order
- POST   /{id}/tasks                Add a task to the breakdown
- PATCH  /{id}/tasks/{task_id}      Update a task
- DELETE /{id}/tasks/{task_id}      Remove a task
- PATCH  /{id}/tasks/{task_id}/status  Transition a task's status
- PATCH  /{id}/status               Transition the repair order status
- GET    /{id}/summary              Compact operational summary
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
from app.repair_orders.schemas import (
    RepairOrderCreate,
    RepairOrderRead,
    RepairOrderSummary,
    RepairOrderUpdate,
    RepairTaskCreate,
    RepairTaskUpdate,
)
from app.repair_orders.services import RepairOrderService

router = APIRouter()


@router.get("/", response_model=PaginatedResponse[RepairOrderRead])
async def list_repair_orders(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    customer_id: UUID | None = Query(None),
    vehicle_id: UUID | None = Query(None),
    technician_id: UUID | None = Query(None),
    advisor_id: UUID | None = Query(None),
    estimate_id: UUID | None = Query(None),
    current_user: User = Depends(require_permission(PermissionEnum.REPAIR_ORDERS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """List repair orders with pagination and filtering."""
    service = RepairOrderService(session)
    orders, total = await service.list_repair_orders(
        page=page,
        size=size,
        status=status,
        customer_id=customer_id,
        vehicle_id=vehicle_id,
        technician_id=technician_id,
        advisor_id=advisor_id,
        estimate_id=estimate_id,
    )
    return PaginatedResponse[RepairOrderRead].create(
        items=[RepairOrderRead.model_validate(o) for o in orders],
        page=page,
        size=size,
        total=total,
    )


@router.post("/", response_model=RepairOrderRead, status_code=status.HTTP_201_CREATED)
@audit(AuditAction.CREATE, "repair_order")
async def create_repair_order(
    order_data: RepairOrderCreate,
    current_user: User = Depends(require_permission(PermissionEnum.REPAIR_ORDERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Create a repair order with an optional task breakdown."""
    service = RepairOrderService(session)
    ro = await service.create_repair_order(order_data)
    return RepairOrderRead.model_validate(ro)


@router.get("/{repair_order_id}", response_model=RepairOrderRead)
async def get_repair_order(
    repair_order_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.REPAIR_ORDERS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get a repair order with its tasks."""
    service = RepairOrderService(session)
    return RepairOrderRead.model_validate(await service.get_by_id(repair_order_id))


@router.get("/{repair_order_id}/summary", response_model=RepairOrderSummary)
async def get_repair_order_summary(
    repair_order_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.REPAIR_ORDERS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get the compact operational summary for a repair order."""
    service = RepairOrderService(session)
    return await service.get_summary(repair_order_id)


@router.patch("/{repair_order_id}", response_model=RepairOrderRead)
@audit(AuditAction.UPDATE, "repair_order")
async def update_repair_order(
    repair_order_id: UUID,
    order_data: RepairOrderUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.REPAIR_ORDERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Update a repair order's header fields."""
    service = RepairOrderService(session)
    ro = await service.update_repair_order(repair_order_id, order_data)
    return RepairOrderRead.model_validate(ro)


@router.delete(
    "/{repair_order_id}", status_code=status.HTTP_204_NO_CONTENT
)
@audit(AuditAction.DELETE, "repair_order")
async def delete_repair_order(
    repair_order_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.REPAIR_ORDERS_MANAGE)),
    session: AsyncSession = Depends(get_session),
):
    """Delete a repair order that has not been worked on."""
    service = RepairOrderService(session)
    await service.delete_repair_order(repair_order_id)


@router.post(
    "/{repair_order_id}/tasks", response_model=RepairOrderRead, status_code=status.HTTP_201_CREATED
)
@audit(
    AuditAction.UPDATE, "repair_order", id_param="repair_order_id",
    summary="Added a task to a repair order",
)
async def add_repair_task(
    repair_order_id: UUID,
    task_data: RepairTaskCreate,
    current_user: User = Depends(require_permission(PermissionEnum.TASKS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Add a task to a repair order's breakdown."""
    service = RepairOrderService(session)
    ro = await service.add_task(repair_order_id, task_data)
    return RepairOrderRead.model_validate(ro)


@router.patch("/{repair_order_id}/tasks/{task_id}", response_model=RepairOrderRead)
@audit(
    AuditAction.UPDATE, "repair_order", id_param="repair_order_id",
    summary="Updated a repair order task",
)
async def update_repair_task(
    repair_order_id: UUID,
    task_id: UUID,
    task_data: RepairTaskUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.TASKS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Update a task's description, assignee or ordering."""
    service = RepairOrderService(session)
    ro = await service.update_task(repair_order_id, task_id, task_data)
    return RepairOrderRead.model_validate(ro)


@router.delete("/{repair_order_id}/tasks/{task_id}", response_model=RepairOrderRead)
@audit(
    AuditAction.UPDATE, "repair_order", id_param="repair_order_id",
    summary="Removed a task from a repair order",
)
async def delete_repair_task(
    repair_order_id: UUID,
    task_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.TASKS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Remove a task from a repair order's breakdown."""
    service = RepairOrderService(session)
    ro = await service.delete_task(repair_order_id, task_id)
    return RepairOrderRead.model_validate(ro)


@router.patch("/{repair_order_id}/tasks/{task_id}/status", response_model=RepairOrderRead)
@audit(
    AuditAction.STATUS_CHANGE, "repair_order", id_param="repair_order_id",
    summary="Changed a task's status on a repair order",
)
async def update_repair_task_status(
    repair_order_id: UUID,
    task_id: UUID,
    status: Annotated[
        str, Query(description="PENDING, IN_PROGRESS, COMPLETED, SKIPPED")
    ],
    notes: str | None = Query(None, max_length=1000),
    current_user: User = Depends(require_permission(PermissionEnum.TASKS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Transition a single task's status."""
    service = RepairOrderService(session)
    ro = await service.update_task_status(repair_order_id, task_id, status, notes)
    return RepairOrderRead.model_validate(ro)


@router.patch("/{repair_order_id}/status", response_model=RepairOrderRead)
@audit(AuditAction.STATUS_CHANGE, "repair_order")
async def update_repair_order_status(
    repair_order_id: UUID,
    status: Annotated[
        str,
        Query(
            description=(
                "DRAFT, APPROVED, IN_PROGRESS, ON_HOLD, COMPLETED, QC_PASSED, "
                "DELIVERED, CANCELLED"
            )
        ),
    ],
    reason: str | None = Query(None, max_length=1000),
    current_user: User = Depends(require_permission(PermissionEnum.REPAIR_ORDERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Transition a repair order to a new status."""
    service = RepairOrderService(session)
    ro = await service.update_status(repair_order_id, status, reason)
    return RepairOrderRead.model_validate(ro)
