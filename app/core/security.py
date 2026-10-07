"""Security utilities for authentication and password handling.

Provides JWT token creation/validation and password hashing using bcrypt.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.core.config import settings

logger = logging.getLogger("autofix.security")

_BCRYPT_ROUNDS = settings.BCRYPT_ROUNDS

# Re-export for convenience
ACCESS_TOKEN_EXPIRE_MINUTES: int = settings.ACCESS_TOKEN_EXPIRE_MINUTES
REFRESH_TOKEN_EXPIRE_DAYS: int = settings.REFRESH_TOKEN_EXPIRE_DAYS
JWT_ALGORITHM: str = settings.JWT_ALGORITHM


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    password_bytes = password.encode("utf-8")
    salt = bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash."""
    try:
        password_bytes = plain_password.encode("utf-8")
        hashed_bytes = hashed_password.encode("utf-8")
        return bcrypt.checkpw(password_bytes, hashed_bytes)
    except (ValueError, TypeError):
        return False


def create_access_token(
    subject: str,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Create a JWT access token.

    Args:
        subject: The user identifier (UUID string).
        expires_delta: Token expiration time. Defaults to settings.
        extra_claims: Additional JWT claims.

    Returns:
        Encoded JWT token string.
    """
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode: dict[str, Any] = {
        "sub": subject,
        "exp": datetime.now(UTC) + expires_delta,
        "type": "access",
        "iat": datetime.now(UTC),
    }
    if extra_claims:
        to_encode.update(extra_claims)

    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(
    subject: str,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a JWT refresh token.

    Args:
        subject: The user identifier (UUID string).
        expires_delta: Token expiration time. Defaults to settings.

    Returns:
        Encoded JWT refresh token string.
    """
    if expires_delta is None:
        expires_delta = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

    to_encode = {
        "sub": subject,
        "exp": datetime.now(UTC) + expires_delta,
        "type": "refresh",
        "iat": datetime.now(UTC),
    }

    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT token.

    Raises:
        JWTError: If token is invalid or expired.
    """
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        raise


def generate_password_reset_token(email: str, expires_minutes: int = 60) -> str:
    """Generate a secure password reset token.

    Args:
        email: The user's email address.
        expires_minutes: Token validity duration.

    Returns:
        Encoded JWT token for password reset.
    """
    expires = timedelta(minutes=expires_minutes)
    to_encode = {
        "sub": f"reset:{email}",
        "exp": datetime.now(UTC) + expires,
        "type": "password_reset",
        "email": email,
        "iat": datetime.now(UTC),
    }
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def verify_password_reset_token(token: str) -> str | None:
    """Verify a password reset token and return the email.

    Returns:
        Email address if valid, None if invalid/expired.
    """
    try:
        payload = decode_token(token)
        if payload.get("type") != "password_reset":
            return None
        email: str = payload.get("email", "")
        return email if email else None
    except JWTError:
        return None
