"""Authentication and authorization business logic.

Handles user authentication, token management, and RBAC lookups.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Permission, Role, RolePermission, User, UserRole
from app.auth.permissions import PermissionEnum, RoleEnum
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    create_access_token,
    create_refresh_token,
    generate_password_reset_token,
    hash_password,
    verify_password,
    verify_password_reset_token,
)

logger = logging.getLogger("autofix.auth.services")


class AuthService:
    """Service for authentication and RBAC operations."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def authenticate(self, email: str, password: str) -> User | None:
        """Authenticate a user by email and password.

        Returns the user if credentials are valid and user is active.
        """
        stmt = select(User).where(func.lower(User.email) == email.lower())
        result = await self.db.execute(stmt)
        user = result.scalar_one_or_none()

        if not user or not user.is_active:
            await asyncio.to_thread(verify_password, password, DUMMY_PASSWORD_HASH)
            logger.warning("Authentication failed for unknown or inactive account")
            return None

        if not await asyncio.to_thread(verify_password, password, user.password_hash):
            logger.warning("Authentication failed due to invalid credentials")
            return None

        return user

    async def get_user_permissions(self, user: User) -> list[str]:
        """Get all permission strings for a user based on their roles."""
        stmt = (
            select(Permission.name)
            .join(RolePermission, Permission.id == RolePermission.permission_id)
            .join(Role, Role.id == RolePermission.role_id)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user.id)
        )
        result = await self.db.execute(stmt)
        return [row[0] for row in result.fetchall()]

    async def get_user_roles(self, user: User) -> list[str]:
        """Get role names for a user."""
        stmt = (
            select(Role.name)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user.id)
        )
        result = await self.db.execute(stmt)
        return [row[0] for row in result.fetchall()]

    async def has_permission(self, user: User, permission: PermissionEnum) -> bool:
        """Check if a user has a specific permission."""
        permissions = await self.get_user_permissions(user)
        return permission.value in permissions

    async def has_any_permission(
        self, user: User, permissions: Sequence[PermissionEnum]
    ) -> bool:
        """Check if a user has any of the specified permissions."""
        user_permissions = await self.get_user_permissions(user)
        return any(p.value in user_permissions for p in permissions)

    async def has_all_permissions(
        self, user: User, permissions: Sequence[PermissionEnum]
    ) -> bool:
        """Check if a user has all of the specified permissions."""
        user_permissions = await self.get_user_permissions(user)
        return all(p.value in user_permissions for p in permissions)

    async def get_user_by_id(self, user_id: UUID | str) -> User | None:
        """Fetch a user by ID with their roles."""
        stmt = select(User).where(User.id == str(user_id))
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_user_by_email(self, email: str) -> User | None:
        """Fetch a user by email."""
        stmt = select(User).where(func.lower(User.email) == email.lower())
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    def generate_tokens(self, user: User) -> tuple[str, str]:
        """Generate access and refresh tokens for a user."""
        access_token = create_access_token(
            subject=str(user.id),
            extra_claims={
                "user_id": str(user.id),
                "email": user.email,
                "ver": user.auth_version,
            },
        )
        refresh_token = create_refresh_token(
            subject=str(user.id),
            extra_claims={"ver": user.auth_version},
        )
        user.refresh_token_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
        return access_token, refresh_token

    def create_password_reset_token(self, email: str) -> str:
        """Generate a password reset token for the given email."""
        return generate_password_reset_token(email)

    async def verify_password_reset_token(self, token: str) -> str | None:
        """Verify a password reset token and return the email if valid."""
        return verify_password_reset_token(token)

    async def reset_password(self, email: str, new_password: str) -> bool:
        """Reset a user's password."""
        stmt = select(User).where(User.email == email)
        result = await self.db.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            return False

        user.password_hash = await asyncio.to_thread(hash_password, new_password)
        user.auth_version += 1
        user.refresh_token_hash = None
        user.password_reset_token_hash = None
        user.password_reset_expires_at = None
        await self.db.commit()
        logger.info("User password reset")
        return True

    async def assign_role(self, user: User, role_name: RoleEnum | str) -> User:
        """Assign a role to a user."""
        role_value = role_name.value if hasattr(role_name, "value") else role_name
        stmt = select(Role).where(Role.name == role_value)
        result = await self.db.execute(stmt)
        role = result.scalar_one_or_none()

        if not role:
            raise ValueError(f"Role {role_name} does not exist")

        existing_ur = await self.db.execute(
            select(UserRole).where(
                UserRole.user_id == user.id,
                UserRole.role_id == role.id,
            )
        )
        if existing_ur.scalar_one_or_none():
            return user

        ur = UserRole(user_id=user.id, role_id=role.id)
        self.db.add(ur)
        await self.db.commit()
        return user
