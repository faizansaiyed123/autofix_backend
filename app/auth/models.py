"""Authentication and RBAC data models.

Defines the User, Role, Permission, and association tables.
Uses UUID v7 primary keys inherited from TimestampedBase.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampedBase


class Role(TimestampedBase):
    """Role entity for RBAC."""

    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    role_permissions_: Mapped[list[RolePermission]] = relationship(
        "RolePermission", back_populates="role", cascade="all, delete-orphan"
    )
    user_roles_: Mapped[list[UserRole]] = relationship(
        "UserRole", back_populates="role", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Role(name={self.name!r})>"


class Permission(TimestampedBase):
    """Permission entity for fine-grained access control."""

    __tablename__ = "permissions"

    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    role_permissions_: Mapped[list[RolePermission]] = relationship(
        "RolePermission", back_populates="permission", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Permission(name={self.name!r})>"


class RolePermission(TimestampedBase):
    """Association table: Role -> Permission (many-to-many)."""

    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False
    )

    role: Mapped[Role] = relationship("Role", back_populates="role_permissions_")
    permission: Mapped[Permission] = relationship("Permission", back_populates="role_permissions_")

    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_role_permission"),
    )


class User(TimestampedBase):
    """User entity - system users (staff) and portal users (customers)."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_verified: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    email_verification_token_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    email_verification_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    password_reset_token_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    password_reset_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refresh_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    auth_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    user_roles_: Mapped[list[UserRole]] = relationship(
        "UserRole", back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def roles(self) -> list[str]:
        """Return list of role names for this user."""
        return [ur.role.name for ur in self.user_roles_]

    @property
    def full_name(self) -> str:
        """Full display name."""
        return f"{self.first_name} {self.last_name}"

    def __repr__(self) -> str:
        return f"<User(email={self.email!r})>"


class UserRole(TimestampedBase):
    """Association table: User -> Role (many-to-many).

    A user can have multiple roles (e.g., technician + parts staff).
    """

    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )

    user: Mapped[User] = relationship("User", back_populates="user_roles_")
    role: Mapped[Role] = relationship("Role", back_populates="user_roles_")

    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uq_user_role"),
    )



class AuthRateLimit(Base):
    """Shared, atomic request counters used to limit auth abuse across workers."""

    __tablename__ = "auth_rate_limits"

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("attempt_count >= 1", name="ck_auth_rate_limits_attempt_count"),
        Index("ix_auth_rate_limits_last_seen_at", "last_seen_at"),
    )
