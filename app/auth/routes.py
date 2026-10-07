"""API routes for authentication.

Endpoints:
- POST /login - User login
- POST /refresh - Refresh access token
- POST /logout - User logout
- POST /password-reset/request - Request password reset
- POST /password-reset/confirm - Confirm password reset
- GET /me - Get current user profile
- GET /permissions - Get current user's permissions
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.audit.decorator import client_ip
from app.audit.models import AuditAction
from app.audit.services import record_auth_event
from app.auth.dependencies import get_current_active_user
from app.auth.schemas import (
    LoginRequest,
    PasswordResetConfirm,
    PasswordResetRequest,
    RefreshRequest,
    TokenResponse,
    UserRead,
)
from app.auth.services import AuthService
from app.core.database import AsyncSession, get_session
from app.core.security import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
)

logger = logging.getLogger("autofix.auth.routes")

router = APIRouter()


@router.post("/login", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def login(
    credentials: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Authenticate user and return access + refresh tokens.

    Both outcomes are audited, including the failed ones. A run of failed
    attempts against one address is the single most useful thing in the log, and
    a log that only records successes cannot show it. The attempted address is
    stored; the password is never anywhere near this code.
    """
    auth_service = AuthService(session)
    user = await auth_service.authenticate(credentials.email, credentials.password)

    if not user:
        await record_auth_event(
            session,
            action=AuditAction.LOGIN_FAILED,
            attempted_email=credentials.email,
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    access_token, refresh_token = auth_service.generate_tokens(user)

    await record_auth_event(
        session,
        action=AuditAction.LOGIN,
        user=user,
        attempted_email=credentials.email,
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/refresh", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def refresh_token(
    refresh_data: RefreshRequest,
    session: AsyncSession = Depends(get_session),
):
    """Refresh access token using a valid refresh token."""
    from jose import JWTError

    from app.core.security import decode_token

    try:
        payload = decode_token(refresh_data.refresh_token)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    token_type = payload.get("type", "")
    if token_type != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
        )

    user_id: str = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    auth_service = AuthService(session)
    user = await auth_service.get_user_by_id(user_id)
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    access_token, refresh_token = auth_service.generate_tokens(user)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/logout", status_code=status.HTTP_200_OK)
async def logout(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(get_current_active_user),
):
    """Logout the current user.

    In a stateless JWT system, logout is handled client-side.
    The client should discard tokens. For server-side invalidation,
    a token blocklist (Redis) would be required.

    The endpoint itself does nothing to the token, but it is still audited: a
    sign-out is a fact about the session, and "when did this person stop using
    the system" is a question an audit log should be able to answer.
    """
    logger.info("User logged out: %s", current_user.email)
    await record_auth_event(
        session,
        action=AuditAction.LOGOUT,
        user=current_user,
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    return {"message": "Successfully logged out"}


@router.get("/me", response_model=UserRead)
async def get_me(
    current_user=Depends(get_current_active_user),
    session: AsyncSession = Depends(get_session),
):
    """Get the current authenticated user's profile."""
    auth_service = AuthService(session)
    roles = await auth_service.get_user_roles(current_user)
    return UserRead(
        id=str(current_user.id),
        email=current_user.email,
        first_name=current_user.first_name,
        last_name=current_user.last_name,
        phone=current_user.phone,
        is_active=current_user.is_active,
        is_staff=current_user.is_staff,
        roles=roles,
    )


@router.get("/permissions")
async def get_current_user_permissions(
    current_user=Depends(get_current_active_user),
    session: AsyncSession = Depends(get_session),
):
    """Get the current user's permissions and roles."""
    auth_service = AuthService(session)
    permissions = await auth_service.get_user_permissions(current_user)
    roles = await auth_service.get_user_roles(current_user)
    return {
        "roles": roles,
        "permissions": permissions,
    }


@router.post("/password-reset/request", status_code=status.HTTP_200_OK)
async def request_password_reset(
    reset_request: PasswordResetRequest,
    session: AsyncSession = Depends(get_session),
):
    """Request a password reset link. Sends email if user exists."""
    auth_service = AuthService(session)
    user = await auth_service.get_user_by_email(reset_request.email)

    if user:
        # The token is generated but not delivered yet: email sending is Phase 17
        # work that is deliberately out of scope. The call is kept so the flow is
        # exercised end to end and the point where delivery hooks in is obvious.
        auth_service.create_password_reset_token(reset_request.email)
        logger.info("Password reset requested for: %s (token generated)", user.email)
        # TODO: send the token by email once a mail transport is configured
    else:
        # Always return success to prevent email enumeration
        logger.info("Password reset requested for unknown email: %s", reset_request.email)

    return {"message": "If the email exists, a password reset link has been sent"}


@router.post("/password-reset/confirm", status_code=status.HTTP_200_OK)
async def confirm_password_reset(
    reset_data: PasswordResetConfirm,
    session: AsyncSession = Depends(get_session),
):
    """Reset password using a valid reset token."""
    auth_service = AuthService(session)
    email = await auth_service.verify_password_reset_token(reset_data.token)

    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token",
        )

    success = await auth_service.reset_password(email, reset_data.new_password)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to reset password",
        )

    return {"message": "Password has been reset successfully"}
