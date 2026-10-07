"""User management business logic.

Handles user CRUD operations and role assignments.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User, UserRole
from app.auth.permissions import RoleEnum
from app.auth.services import AuthService
from app.common.exceptions import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
)
from app.core.security import hash_password
from app.users.schemas import UserCreate, UserUpdate

logger = logging.getLogger("autofix.users.services")


class UserService:
    """Service for user management operations."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.auth_service = AuthService(db)

    async def get_by_id(self, user_id: UUID | str) -> User:
        """Get a user by ID."""
        stmt = select(User).where(User.id == str(user_id))
        result = await self.db.execute(stmt)
        user = result.scalar_one_or_none()
        if not user:
            raise NotFoundError(f"User with id {user_id} not found")
        return user

    async def get_by_email(self, email: str) -> User | None:
        """Get a user by email."""
        stmt = select(User).where(User.email == email)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_users(
        self,
        *,
        page: int = 1,
        size: int = 20,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> tuple[list[User], int]:
        """List users with optional filtering and pagination."""
        stmt = select(User)
        count_stmt = select(User)

        if is_active is not None:
            stmt = stmt.where(User.is_active == is_active)
            count_stmt = count_stmt.where(User.is_active == is_active)

        if search:
            like_pattern = f"%{search}%"
            stmt = stmt.where(
                (User.email.ilike(like_pattern))
                | (User.first_name.ilike(like_pattern))
                | (User.last_name.ilike(like_pattern))
            )
            count_stmt = count_stmt.where(
                (User.email.ilike(like_pattern))
                | (User.first_name.ilike(like_pattern))
                | (User.last_name.ilike(like_pattern))
            )

        count_result = await self.db.execute(count_stmt)
        total = len(count_result.fetchall())

        offset = (page - 1) * size
        stmt = stmt.order_by(User.created_at.desc()).offset(offset).limit(size)
        result = await self.db.execute(stmt)
        users = result.scalars().all()

        return users, total

    async def create_user(self, user_data: UserCreate, requesting_user: User | None = None) -> User:
        """Create a new user."""
        existing = await self.get_by_email(user_data.email)
        if existing:
            raise ConflictError(f"User with email {user_data.email} already exists")

        user = User(
            email=user_data.email,
            password_hash=hash_password(user_data.password),
            first_name=user_data.first_name,
            last_name=user_data.last_name,
            phone=user_data.phone,
            is_active=user_data.is_active,
            is_staff=user_data.is_staff,
        )
        self.db.add(user)
        await self.db.flush()

        # Assign roles
        for role_name in user_data.roles:
            await self.auth_service.assign_role(user, role_name)

        await self.db.commit()
        await self.db.refresh(user)
        logger.info(
            "User created: %s by %s", user.email, requesting_user.email if requesting_user else "system"
        )
        return user

    async def update_user(self, user_id: UUID | str, user_data: UserUpdate, requesting_user: User | None = None) -> User:
        """Update an existing user."""
        user = await self.get_by_id(user_id)

        update_data = user_data.model_dump(exclude_unset=True)

        # Handle roles separately
        roles = update_data.pop("roles", None)

        if "password" in update_data:
            update_data["password_hash"] = hash_password(update_data.pop("password"))

        # `exclude_unset` already narrowed this to the fields the caller actually
        # sent, so every key here is a deliberate change -- including an explicit
        # null, which is how a nullable field like `phone` gets cleared. Testing
        # `value is not None` here would collapse "cleared it" back into "left it
        # alone" and make those fields impossible to empty from the UI.
        for field, value in update_data.items():
            setattr(user, field, value)

        if roles is not None:
            # Remove existing roles
            await self.db.execute(
                UserRole.__table__.delete().where(UserRole.user_id == user.id)
            )
            # Add new roles
            for role_name in roles:
                await self.auth_service.assign_role(user, role_name)

        await self.db.commit()
        await self.db.refresh(user)
        logger.info(
            "User updated: %s by %s", user.email, requesting_user.email if requesting_user else "system"
        )
        return user

    async def delete_user(self, user_id: UUID | str, requesting_user: User | None = None) -> None:
        """Delete a user (soft delete by deactivating)."""
        user = await self.get_by_id(user_id)

        if user.is_staff and str(user.id) == str(requesting_user.id if requesting_user else ""):
            raise BusinessRuleError("You cannot deactivate your own account")

        user.is_active = False
        await self.db.commit()
        logger.info(
            "User deactivated: %s by %s", user.email, requesting_user.email if requesting_user else "system"
        )

    async def set_roles(self, user_id: UUID | str, role_names: list[RoleEnum | str]) -> User:
        """Set roles for a user (replaces existing roles)."""
        user = await self.get_by_id(user_id)

        await self.db.execute(
            UserRole.__table__.delete().where(UserRole.user_id == user.id)
        )
        for role_name in role_names:
            await self.auth_service.assign_role(user, role_name)

        await self.db.commit()
        await self.db.refresh(user)
        return user
