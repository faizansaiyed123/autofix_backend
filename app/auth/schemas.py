"""Pydantic schemas for authentication endpoints."""

from __future__ import annotations

from pydantic import Field

from app.common.schemas import BaseSchema


class LoginRequest(BaseSchema):
    email: str = Field(..., description="User email address")
    password: str = Field(..., min_length=1, description="User password")


class TokenResponse(BaseSchema):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class RefreshRequest(BaseSchema):
    refresh_token: str


class PasswordResetRequest(BaseSchema):
    email: str = Field(..., description="Email address to reset password for")


class PasswordResetConfirm(BaseSchema):
    token: str
    new_password: str = Field(..., min_length=8, description="New password (min 8 characters)")


class UserRead(BaseSchema):
    id: str
    email: str
    first_name: str
    last_name: str
    phone: str | None = None
    is_active: bool
    is_staff: bool
    roles: list[str] = Field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class UserBasicRead(BaseSchema):
    id: str
    email: str
    first_name: str
    last_name: str
