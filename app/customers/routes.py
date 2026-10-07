"""API routes for customer management.

Endpoints:
- GET / - List customers (paginated)
- POST / - Create a customer
- GET /search - Search customers
- GET /{id} - Get a customer by ID
- PATCH /{id} - Update a customer
- DELETE /{id} - Deactivate a customer
- GET /{id}/vehicles - Get customer's vehicles
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
from app.customers.schemas import CustomerCreate, CustomerRead, CustomerUpdate
from app.customers.services import CustomerService

router = APIRouter()


@router.get("/", response_model=PaginatedResponse[CustomerRead])
async def list_customers(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None),
    customer_status: str | None = Query(None),
    current_user: User = Depends(require_permission(PermissionEnum.CUSTOMERS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """List customers with pagination and optional filtering."""
    service = CustomerService(session)
    customers, total = await service.list_customers(
        page=page, size=size, search=search, customer_status=customer_status
    )

    customer_list = [CustomerRead.model_validate(c) for c in customers]

    pages = (total + size - 1) // size if size > 0 else 0
    return PaginatedResponse[CustomerRead](
        data=customer_list,
        meta=PaginationMeta(page=page, size=size, total=total, pages=pages),
    )


@router.post("/", response_model=CustomerRead, status_code=status.HTTP_201_CREATED)
@audit(AuditAction.CREATE, "customer")
async def create_customer(
    customer_data: CustomerCreate,
    current_user: User = Depends(require_permission(PermissionEnum.CUSTOMERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Create a new customer."""
    service = CustomerService(session)
    customer = await service.create_customer(customer_data)
    return CustomerRead.model_validate(customer)


@router.get("/search", response_model=list[CustomerRead])
async def search_customers(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(require_permission(PermissionEnum.CUSTOMERS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Search customers by name, email, phone, or company."""
    service = CustomerService(session)
    results = await service.search(q, limit=limit)
    return [CustomerRead.model_validate(c) for c in results]


@router.get("/{customer_id}", response_model=CustomerRead)
async def get_customer(
    customer_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.CUSTOMERS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get a customer by ID."""
    service = CustomerService(session)
    customer = await service.get_by_id(customer_id)
    return CustomerRead.model_validate(customer)


@router.patch("/{customer_id}", response_model=CustomerRead)
@audit(AuditAction.UPDATE, "customer")
async def update_customer(
    customer_id: UUID,
    customer_data: CustomerUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.CUSTOMERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Update a customer."""
    service = CustomerService(session)
    customer = await service.update_customer(customer_id, customer_data)
    return CustomerRead.model_validate(customer)


@router.delete("/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
@audit(AuditAction.DELETE, "customer", summary="Deactivated a customer")
async def delete_customer(
    customer_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.CUSTOMERS_MANAGE)),
    session: AsyncSession = Depends(get_session),
):
    """Deactivate a customer (soft delete)."""
    service = CustomerService(session)
    await service.delete_customer(customer_id)


@router.get("/{customer_id}/vehicles")
async def get_customer_vehicles(
    customer_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.CUSTOMERS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get all vehicles for a customer."""
    from app.vehicles.schemas import VehicleRead

    service = CustomerService(session)
    # Raises NotFoundError for an unknown or inactive customer, so a missing id
    # returns 404 rather than an empty list.
    await service.get_by_id(customer_id)
    vehicles = await service.get_customer_vehicles(customer_id)
    return [VehicleRead.model_validate(v) for v in vehicles]
