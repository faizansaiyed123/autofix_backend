"""FastAPI dependencies for authentication and authorization.

Provides:
- get_current_user: Extract and validate the current user from JWT token
- get_current_active_user: Ensure user is active
- require_permission: Dependency that checks if user has required permissions
- require_role: Dependency that checks if user has a required role
"""

from __future__ import annotations

import logging

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.models import User
from app.auth.permissions import PermissionEnum, RoleEnum
from app.auth.services import AuthService
from app.common.exceptions import (
    AuthenticationError,
    PermissionDeniedError,
)
from app.core.database import AsyncSession, get_session

logger = logging.getLogger("autofix.auth.deps")

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Extract and validate the current user from the JWT token.

    Raises:
        AuthenticationError: If no token, invalid token, or user not found.
    """
    from jose import JWTError

    from app.core.security import decode_token

    if not credentials:
        raise AuthenticationError("Authentication required")

    token = credentials.credentials
    try:
        payload = decode_token(token)
    except JWTError:
        raise AuthenticationError("Invalid or expired token")

    user_id: str = payload.get("sub")
    token_type: str = payload.get("type", "")

    if not user_id or token_type != "access":
        raise AuthenticationError("Invalid token type")

    auth_service = AuthService(session)
    user = await auth_service.get_user_by_id(user_id)

    if not user:
        raise AuthenticationError("User not found")

    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Ensure the current user is active."""
    if not current_user.is_active:
        raise AuthenticationError("User account is disabled")
    return current_user


def require_permission(*permissions: PermissionEnum):
    """Dependency that checks if the current user has at least one of the required permissions.

    The OWNER role bypasses all permission checks.

    Usage:
        @router.get("/customers")
        async def list_customers(
            current_user: User = Depends(require_permission(PermissionEnum.CUSTOMERS_READ)),
            session: AsyncSession = Depends(get_session),
        ):
            ...
    """

    async def permission_dependency(
        current_user: User = Depends(get_current_active_user),
        session: AsyncSession = Depends(get_session),
    ) -> User:
        auth_service = AuthService(session)
        user_roles = await auth_service.get_user_roles(current_user)

        if RoleEnum.OWNER.value in user_roles:
            return current_user

        user_permissions = await auth_service.get_user_permissions(current_user)

        if not any(p.value in user_permissions for p in permissions):
            raise PermissionDeniedError(
                "You do not have permission to perform this action"
            )

        return current_user

    return permission_dependency


def require_staff():
    """Dependency that refuses anyone who is not shop staff.

    A customer signs in with the same machinery as everybody else and holds real
    permissions — ``vehicles:read``, ``invoices:read``, ``payments:write`` —
    because the portal needs them to see their own account. Those same
    permissions guard the shop's own endpoints, and every one of those is
    shop-wide: ``GET /api/v1/invoices/`` is the invoice book, not the caller's.
    A permission says *what kind of work* a caller may do; on its own it says
    nothing about *whose rows* they may see.

    So the shop's routers sit behind this gate, and a customer's own data is
    reached through the portal, which derives the customer from the token and
    scopes every query to it.

    Staffness is decided by roles, not by ``User.is_staff``. That column is
    writable through the users API, and a boolean a caller can set is not
    something to hang "who may read the shop's books" on: a technician created
    without the box ticked would be locked out of the shop, and a customer
    created with it ticked would be let in.
    """

    async def staff_dependency(
        current_user: User = Depends(get_current_active_user),
        session: AsyncSession = Depends(get_session),
    ) -> User:
        auth_service = AuthService(session)
        user_roles = await auth_service.get_user_roles(current_user)

        if not any(role != RoleEnum.CUSTOMER.value for role in user_roles):
            raise PermissionDeniedError(
                "This is a shop endpoint. Your own account is at /api/v1/portal"
            )

        return current_user

    return staff_dependency


def require_role(*roles: RoleEnum):
    """Dependency that checks if the current user has at least one of the required roles.

    The OWNER role bypasses all role checks.

    Usage:
        @router.get("/admin")
        async def admin_endpoint(
            current_user: User = Depends(require_role(RoleEnum.OWNER)),
        ):
            ...
    """

    async def role_dependency(
        current_user: User = Depends(get_current_active_user),
        session: AsyncSession = Depends(get_session),
    ) -> User:
        auth_service = AuthService(session)
        user_roles = await auth_service.get_user_roles(current_user)

        if RoleEnum.OWNER.value in user_roles:
            return current_user

        if not any(role.value in user_roles for role in roles):
            raise PermissionDeniedError(
                "You do not have the required role to access this resource"
            )

        return current_user

    return role_dependency
