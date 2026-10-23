"""Pydantic schemas for authentication endpoints."""

from __future__ import annotations

import re

from pydantic import Field, field_validator

from app.common.schemas import BaseSchema


def _normalize_email(value: str) -> str:
    value = value.lower().strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        raise ValueError("Invalid email address")
    return value


class LoginRequest(BaseSchema):
    email: str = Field(..., min_length=3, max_length=255, description="User email address")
    password: str = Field(..., min_length=1, max_length=72, description="User password")

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return _normalize_email(value)

    @field_validator("password")
    @classmethod
    def validate_password_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password must be no longer than 72 UTF-8 bytes")
        return value


class CustomerRegistrationRequest(BaseSchema):
    """Public signup payload for a customer portal account."""

    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=8, max_length=72)
    phone: str | None = Field(None, max_length=30)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return _normalize_email(value)

    @field_validator("password")
    @classmethod
    def validate_password_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password must be no longer than 72 UTF-8 bytes")
        return value

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str | None) -> str | None:
        return value or None


class EmailVerificationRequest(BaseSchema):
    token: str = Field(..., min_length=20, max_length=200)


class TokenResponse(BaseSchema):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseSchema):
    refresh_token: str = Field(..., min_length=1, max_length=4096)


class PasswordResetRequest(BaseSchema):
    email: str = Field(..., min_length=3, max_length=255, description="Email address")

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return _normalize_email(value)


class PasswordResetConfirm(BaseSchema):
    token: str = Field(..., min_length=20, max_length=4096)
    new_password: str = Field(..., min_length=8, max_length=72)

    @field_validator("new_password")
    @classmethod
    def validate_password_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password must be no longer than 72 UTF-8 bytes")
        return value


class UserRead(BaseSchema):
    id: str
    email: str
    first_name: str
    last_name: str
    phone: str | None = None
    is_active: bool
    is_staff: bool
    email_verified: bool = True
    roles: list[str] = Field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class UserBasicRead(BaseSchema):
    id: str
    email: str
    first_name: str
    last_name: str
