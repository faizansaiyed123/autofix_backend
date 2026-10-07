"""API routes for user management.

Endpoints:
- GET / - List users (paginated)
- POST / - Create a user
- GET /{id} - Get a user by ID
- PATCH /{id} - Update a user
- DELETE /{id} - Deactivate a user
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.auth.dependencies import require_permission
from app.auth.models import User
from app.auth.permissions import PermissionEnum
from app.common.schemas import PaginatedResponse, PaginationMeta
from app.core.database import AsyncSession, get_session
from app.users.schemas import UserCreate, UserRead, UserUpdate
from app.users.services import UserService

router = APIRouter()


@router.get("/", response_model=PaginatedResponse[UserRead])
async def list_users(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None),
    is_active: bool | None = Query(None),
    current_user: User = Depends(require_permission(PermissionEnum.USERS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """List users with pagination and optional filtering."""
    service = UserService(session)
    users, total = await service.list_users(
        page=page, size=size, is_active=is_active, search=search
    )

    user_list = []
    for user in users:
        roles = await service.auth_service.get_user_roles(user)
        user_list.append(UserRead(
            id=str(user.id),
            email=user.email,
            first_name=user.first_name,
            last_name=user.last_name,
            phone=user.phone,
            is_active=user.is_active,
            is_staff=user.is_staff,
            roles=roles,
        ))

    pages = (total + size - 1) // size if size > 0 else 0
    return PaginatedResponse[UserRead](
        data=user_list,
        meta=PaginationMeta(page=page, size=size, total=total, pages=pages),
    )


@router.post("/", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def create_user(
    user_data: UserCreate,
    current_user: User = Depends(require_permission(PermissionEnum.USERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Create a new user."""
    service = UserService(session)
    user = await service.create_user(user_data, requesting_user=current_user)
    roles = await service.auth_service.get_user_roles(user)
    return UserRead(
        id=str(user.id),
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        phone=user.phone,
        is_active=user.is_active,
        is_staff=user.is_staff,
        roles=roles,
    )


@router.get("/{user_id}", response_model=UserRead)
async def get_user(
    user_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.USERS_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Get a user by ID."""
    service = UserService(session)
    user = await service.get_by_id(user_id)
    roles = await service.auth_service.get_user_roles(user)
    return UserRead(
        id=str(user.id),
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        phone=user.phone,
        is_active=user.is_active,
        is_staff=user.is_staff,
        roles=roles,
    )


@router.patch("/{user_id}", response_model=UserRead)
async def update_user(
    user_id: UUID,
    user_data: UserUpdate,
    current_user: User = Depends(require_permission(PermissionEnum.USERS_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """Update a user."""
    service = UserService(session)
    user = await service.update_user(user_id, user_data, requesting_user=current_user)
    roles = await service.auth_service.get_user_roles(user)
    return UserRead(
        id=str(user.id),
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        phone=user.phone,
        is_active=user.is_active,
        is_staff=user.is_staff,
        roles=roles,
    )


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: UUID,
    current_user: User = Depends(require_permission(PermissionEnum.USERS_MANAGE)),
    session: AsyncSession = Depends(get_session),
):
    """Deactivate a user (soft delete)."""
    service = UserService(session)
    await service.delete_user(user_id, requesting_user=current_user)
