"""Pydantic schemas for customer management."""

from __future__ import annotations

import uuid

from pydantic import Field, field_validator

from app.common.schemas import BaseSchema
from app.customers.models import AddressType, ContactMethod, CustomerStatus


class AddressBase(BaseSchema):
    street: str = Field(..., min_length=1, max_length=255)
    city: str = Field(..., min_length=1, max_length=100)
    state: str | None = Field(None, max_length=50)
    postal_code: str | None = Field(None, max_length=20)
    country: str = Field("US", max_length=50)
    address_type: AddressType = AddressType.PRIMARY


class AddressCreate(AddressBase):
    pass


class AddressRead(AddressBase):
    id: uuid.UUID


class CustomerBase(BaseSchema):
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    company_name: str | None = Field(None, max_length=200)
    email: str | None = Field(None, max_length=255)
    phone: str | None = Field(None, max_length=30)
    preferred_contact: ContactMethod = ContactMethod.EMAIL
    customer_status: CustomerStatus = CustomerStatus.ACTIVE
    notes: str | None = Field(None, max_length=2000)


class CustomerCreate(CustomerBase):
    addresses: list[AddressCreate] | None = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.lower().strip()
            if "@" not in v:
                raise ValueError("Invalid email address")
        return v


class CustomerUpdate(BaseSchema):
    first_name: str | None = Field(None, max_length=100)
    last_name: str | None = Field(None, max_length=100)
    company_name: str | None = Field(None, max_length=200)
    email: str | None = Field(None, max_length=255)
    phone: str | None = Field(None, max_length=30)
    preferred_contact: ContactMethod | None = None
    customer_status: CustomerStatus | None = None
    notes: str | None = Field(None, max_length=2000)
    addresses: list[AddressCreate] | None = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.lower().strip()
            if "@" not in v:
                raise ValueError("Invalid email address")
        return v


class CustomerRead(CustomerBase):
    id: uuid.UUID
    user_id: uuid.UUID | None = None
    addresses: list[AddressRead] = Field(default_factory=list)

    @property
    def full_name(self) -> str:
        name = f"{self.first_name} {self.last_name}"
        if self.company_name:
            name = f"{self.company_name} ({name})"
        return name
