"""API routes for authentication.

Endpoints:
- POST /register - Create a customer portal account
- POST /login - User login
- POST /refresh - Refresh access token
- POST /logout - User logout
- POST /password-reset/request - Request password reset
- POST /password-reset/confirm - Confirm password reset
- GET /me - Get current user profile
- GET /permissions - Get current user's permissions
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.audit.decorator import client_ip
from app.audit.models import AuditAction
from app.audit.services import record_auth_event
from app.auth.dependencies import get_current_active_user
from app.auth.models import Role, User, UserRole
from app.auth.permissions import RoleEnum
from app.auth.rate_limit import client_address, enforce_rate_limit
from app.auth.schemas import (
    CustomerRegistrationRequest,
    EmailVerificationRequest,
    LoginRequest,
    PasswordResetConfirm,
    PasswordResetRequest,
    RefreshRequest,
    TokenResponse,
    UserRead,
)
from app.auth.services import AuthService
from app.core.email import (
    email_delivery_configured,
    queue_password_reset_email,
    queue_verification_email,
)
from app.customers.models import ContactMethod, Customer, CustomerStatus
from app.core.database import AsyncSession, get_session
from app.core.config import settings
from app.core.security import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    hash_password,
)

logger = logging.getLogger("autofix.auth.routes")

router = APIRouter()

_AUTH_ENUMERATION_RESPONSE_FLOOR_SECONDS = 0.2


async def _pad_enumeration_safe_response(started_at: float) -> None:
    """Apply a small asynchronous minimum duration to generic account-recovery responses."""
    remaining = _AUTH_ENUMERATION_RESPONSE_FLOOR_SECONDS - (time.monotonic() - started_at)
    if remaining > 0:
        await asyncio.sleep(remaining)


@router.post(
    "/register",
    response_model=dict[str, str],
    status_code=status.HTTP_202_ACCEPTED,
)
async def register_customer(
    registration: CustomerRegistrationRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Register a customer account; it cannot sign in until email verification succeeds."""
    ip = client_address(request)
    await enforce_rate_limit(session, scope="register-ip", identity=ip, limit=30, window_seconds=3600)
    await enforce_rate_limit(session, scope="register-email", identity=registration.email, limit=5, window_seconds=3600)
    if not email_delivery_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email verification is temporarily unavailable. Please try again later.",
        )

    # Hash before account-existence lookups so duplicate-email responses do not
    # skip the expensive password-verification cost and become a timing oracle.
    password_hash = await asyncio.to_thread(hash_password, registration.password)
    generic_message = "If this email can be registered, a verification message will be sent."
    existing_user = await session.execute(
        select(User.id).where(func.lower(User.email) == registration.email).limit(1)
    )
    if existing_user.scalar_one_or_none():
        return {"message": generic_message}

    existing_customer = await session.execute(
        select(Customer.id).where(func.lower(Customer.email) == registration.email).limit(1)
    )
    if existing_customer.scalar_one_or_none():
        # Never attach an existing workshop record solely because a caller knows its email.
        return {"message": generic_message}

    role_result = await session.execute(select(Role).where(Role.name == RoleEnum.CUSTOMER.value))
    customer_role = role_result.scalar_one_or_none()
    if customer_role is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Customer registration is not available right now.",
        )

    user = User(
        email=registration.email,
        password_hash=password_hash,
        first_name=registration.first_name,
        last_name=registration.last_name,
        phone=registration.phone,
        is_active=True,
        is_staff=False,
        email_verified=False,
    )
    session.add(user)
    try:
        # All three records must succeed or none of them should be created.
        await session.flush()
        session.add(UserRole(user_id=user.id, role_id=customer_role.id))
        session.add(Customer(
            first_name=registration.first_name,
            last_name=registration.last_name,
            email=registration.email,
            phone=registration.phone,
            preferred_contact=ContactMethod.EMAIL.value,
            customer_status=CustomerStatus.ACTIVE.value,
            user_id=user.id,
        ))
        await queue_verification_email(session, user.id)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        # Concurrent duplicate registrations are intentionally indistinguishable from success.
        return {"message": generic_message}

    return {"message": generic_message}


@router.post("/verify-email", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def verify_customer_email(
    verification: EmailVerificationRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Consume a single-use verification token and establish the customer session."""
    await enforce_rate_limit(
        session, scope="verify-email-ip", identity=client_address(request), limit=100, window_seconds=3600
    )
    token_hash = hashlib.sha256(verification.token.encode("utf-8")).hexdigest()
    now = datetime.now(UTC)
    result = await session.execute(
        select(User).where(
            User.email_verification_token_hash == token_hash,
            User.email_verification_expires_at > now,
            User.email_verified.is_(False),
            User.is_active.is_(True),
        ).with_for_update().execution_options(populate_existing=True)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This verification link is invalid or expired. Request a new one.",
        )
    user.email_verified = True
    user.email_verification_token_hash = None
    user.email_verification_expires_at = None
    access_token, refresh_token = AuthService(session).generate_tokens(user)
    await session.commit()
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/verification/resend", response_model=dict[str, str], status_code=status.HTTP_202_ACCEPTED)
async def resend_verification_email(
    reset_request: PasswordResetRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Resend verification without revealing whether an account exists."""
    started_at = time.monotonic()
    ip = client_address(request)
    await enforce_rate_limit(session, scope="resend-verification-ip", identity=ip, limit=30, window_seconds=3600)
    await enforce_rate_limit(
        session, scope="resend-verification-email", identity=reset_request.email, limit=5, window_seconds=3600
    )
    if not email_delivery_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email verification is temporarily unavailable. Please try again later.",
        )
    user = await AuthService(session).get_user_by_email(reset_request.email)
    if user and user.is_active and not user.email_verified:
        user.email_verification_token_hash = None
        user.email_verification_expires_at = None
        await queue_verification_email(session, user.id)
        await session.commit()
    await _pad_enumeration_safe_response(started_at)
    return {"message": "If the account exists and needs verification, an email will be sent."}


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
    ip = client_address(request)
    await enforce_rate_limit(
        session,
        scope="login-ip",
        identity=ip,
        limit=settings.AUTH_LOGIN_IP_RATE_LIMIT,
        window_seconds=900,
    )
    await enforce_rate_limit(
        session,
        scope="login-email",
        identity=credentials.email,
        limit=settings.AUTH_LOGIN_EMAIL_RATE_LIMIT,
        window_seconds=900,
    )
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
            detail="Invalid email or password. If you recently registered, verify your email before signing in.",
        )

    if not user.email_verified:
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
            detail="Invalid email or password. If you recently registered, verify your email before signing in.",
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
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Refresh access token using a valid, unrotated refresh token."""
    await enforce_rate_limit(
        session, scope="refresh-ip", identity=client_address(request), limit=120, window_seconds=900
    )
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
    locked_user = await session.execute(
        select(User).where(User.id == user_id).with_for_update()
    )
    user = locked_user.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    if payload.get("ver", 0) != user.auth_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked. Please sign in again.",
        )
    supplied_hash = hashlib.sha256(refresh_data.refresh_token.encode("utf-8")).hexdigest()
    if user.refresh_token_hash is not None and not hmac.compare_digest(user.refresh_token_hash, supplied_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has already been rotated or revoked.",
        )
    if not user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please verify your email address before signing in.",
        )

    access_token, refresh_token = auth_service.generate_tokens(user)
    await session.commit()

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
    """Revoke the account's current token generation and record the sign-out.

    Incrementing the account version invalidates existing access and refresh
    tokens; the frontend also discards its locally stored token pair.
    """
    logger.info("User logged out")
    await record_auth_event(
        session,
        action=AuditAction.LOGOUT,
        user=current_user,
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    current_user.auth_version += 1
    current_user.refresh_token_hash = None
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
        email_verified=current_user.email_verified,
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
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Request a one-time password reset; never disclose whether the email exists."""
    started_at = time.monotonic()
    ip = client_address(request)
    await enforce_rate_limit(session, scope="password-reset-ip", identity=ip, limit=50, window_seconds=3600)
    await enforce_rate_limit(
        session, scope="password-reset-email", identity=reset_request.email, limit=5, window_seconds=3600
    )
    if not email_delivery_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Password recovery is temporarily unavailable. Please try again later.",
        )
    auth_service = AuthService(session)
    user = await auth_service.get_user_by_email(reset_request.email)
    if user and user.is_active:
        # A new recovery request invalidates any older reset link immediately.
        user.password_reset_token_hash = None
        user.password_reset_expires_at = None
        await queue_password_reset_email(session, user.id)
        await session.commit()
    await _pad_enumeration_safe_response(started_at)
    return {"message": "If the email exists, a password reset link has been sent"}


@router.post("/password-reset/confirm", status_code=status.HTTP_200_OK)
async def confirm_password_reset(
    reset_data: PasswordResetConfirm,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Consume a single-use reset token, change the password, and revoke old sessions."""
    await enforce_rate_limit(
        session, scope="password-reset-confirm-ip", identity=client_address(request), limit=30, window_seconds=3600
    )
    auth_service = AuthService(session)
    email = await auth_service.verify_password_reset_token(reset_data.token)
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token",
        )

    token_hash = hashlib.sha256(reset_data.token.encode("utf-8")).hexdigest()
    now = datetime.now(UTC)
    result = await session.execute(
        select(User)
        .where(func.lower(User.email) == email.lower())
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    user = result.scalar_one_or_none()
    if (
        not user
        or not user.password_reset_token_hash
        or not hmac.compare_digest(user.password_reset_token_hash, token_hash)
        or not user.password_reset_expires_at
        or user.password_reset_expires_at <= now
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token",
        )

    user.password_hash = await asyncio.to_thread(hash_password, reset_data.new_password)
    user.auth_version += 1
    user.refresh_token_hash = None
    user.password_reset_token_hash = None
    user.password_reset_expires_at = None
    await session.commit()
    return {"message": "Password has been reset successfully"}
