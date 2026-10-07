"""Customer and Address data models.

Customers can be individuals or businesses. They may optionally be
linked to a User account for portal access.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TimestampedBase


class CustomerStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    SUSPENDED = "SUSPENDED"


class ContactMethod(str, enum.Enum):
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    SMS = "SMS"


class AddressType(str, enum.Enum):
    PRIMARY = "PRIMARY"
    BILLING = "BILLING"
    OTHER = "OTHER"


class Address(TimestampedBase):
    """Address model - can be linked to a Customer."""

    __tablename__ = "addresses"

    street: Mapped[str] = mapped_column(String(255), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str | None] = mapped_column(String(50), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str] = mapped_column(String(50), default="US", nullable=False)
    address_type: Mapped[str] = mapped_column(
        String(20), default=AddressType.PRIMARY.value, nullable=False
    )

    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id"), nullable=True, index=True
    )

    customer: Mapped[Customer | None] = relationship(
        "Customer", back_populates="addresses", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Address({self.street}, {self.city}, {self.state})>"


class Customer(TimestampedBase):
    """Customer entity - business data for a customer.

    May optionally link to a User account for portal access.
    A customer can own multiple vehicles.
    """

    __tablename__ = "customers"

    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    company_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)

    preferred_contact: Mapped[str] = mapped_column(
        String(20), default=ContactMethod.EMAIL.value, nullable=False
    )
    customer_status: Mapped[str] = mapped_column(
        String(20), default=CustomerStatus.ACTIVE.value, nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional link to a User account (for portal access)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    addresses: Mapped[list[Address]] = relationship(
        "Address", back_populates="customer", cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Customer({self.first_name} {self.last_name})>"

    @property
    def full_name(self) -> str:
        """Full display name of the customer."""
        name = f"{self.first_name} {self.last_name}"
        if self.company_name:
            name = f"{self.company_name} ({name})"
        return name
