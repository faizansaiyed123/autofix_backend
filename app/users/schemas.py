"""Pydantic schemas for user management endpoints."""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from app.auth.permissions import RoleEnum
from app.common.schemas import BaseSchema


class UserBase(BaseSchema):
    email: str = Field(..., max_length=255)
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    phone: str | None = Field(None, max_length=30)
    is_active: bool = True
    is_staff: bool = False


class UserCreate(UserBase):
    password: str = Field(..., min_length=8, max_length=128)
    roles: list[RoleEnum] = Field(default_factory=list)

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.lower().strip()
        if "@" not in v:
            raise ValueError("Invalid email address")
        return v


class UserUpdate(BaseSchema):
    email: str | None = Field(None, max_length=255)
    first_name: str | None = Field(None, max_length=100)
    last_name: str | None = Field(None, max_length=100)
    phone: str | None = Field(None, max_length=30)
    is_active: bool | None = None
    is_staff: bool | None = None
    roles: list[RoleEnum] | None = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.lower().strip()
            if "@" not in v:
                raise ValueError("Invalid email address")
        return v

    @model_validator(mode="after")
    def reject_null_on_required_columns(self) -> UserUpdate:
        """Refuse `null` for columns that are NOT NULL in the database.

        The service applies every field the caller actually sent, which is what
        lets a nullable field such as `phone` be cleared. These columns cannot
        be cleared, and without this check an explicit null would reach the ORM
        as a real NULL and come back as a 500 from the integrity constraint
        rather than a 422 that names the offending field.
        """
        for name in ("email", "first_name", "last_name", "is_active", "is_staff"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be set to null")
        return self


class UserRead(BaseSchema):
    id: str
    email: str
    first_name: str
    last_name: str
    phone: str | None = None
    is_active: bool
    is_staff: bool
    roles: list[str] = Field(default_factory=list)
