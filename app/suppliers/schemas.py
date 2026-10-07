"""Pydantic schemas for suppliers."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import Field, field_validator

from app.common.schemas import BaseSchema
from app.suppliers.models import SupplierStatus


def _clean_optional(value: str | None) -> str | None:
    """Treat a whitespace-only string as absent rather than as a value."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class SupplierCreate(BaseSchema):
    """Payload for adding a supplier."""

    name: str = Field(..., min_length=1, max_length=200)
    contact_name: str | None = Field(None, max_length=200)
    email: str | None = Field(None, max_length=200)
    phone: str | None = Field(None, max_length=50)
    address_line1: str | None = Field(None, max_length=200)
    address_line2: str | None = Field(None, max_length=200)
    city: str | None = Field(None, max_length=100)
    state: str | None = Field(None, max_length=100)
    postal_code: str | None = Field(None, max_length=20)
    country: str | None = Field(None, max_length=100)
    account_number: str | None = Field(None, max_length=50)
    website: str | None = Field(None, max_length=200)
    lead_time_days: int = Field(0, ge=0, le=365)
    payment_terms: str | None = Field(None, max_length=100)
    notes: str | None = Field(None, max_length=2000)
    is_preferred: bool = False

    @field_validator(
        "contact_name",
        "email",
        "phone",
        "address_line1",
        "address_line2",
        "city",
        "state",
        "postal_code",
        "country",
        "account_number",
        "website",
        "payment_terms",
        "notes",
    )
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        return _clean_optional(v)


class SupplierUpdate(BaseSchema):
    """Partial update of a supplier.

    ``name`` is editable: unlike a part number, a supplier's name is not
    referenced by past documents, and a trading name change is a normal event.
    """

    name: str | None = Field(None, min_length=1, max_length=200)
    contact_name: str | None = Field(None, max_length=200)
    email: str | None = Field(None, max_length=200)
    phone: str | None = Field(None, max_length=50)
    address_line1: str | None = Field(None, max_length=200)
    address_line2: str | None = Field(None, max_length=200)
    city: str | None = Field(None, max_length=100)
    state: str | None = Field(None, max_length=100)
    postal_code: str | None = Field(None, max_length=20)
    country: str | None = Field(None, max_length=100)
    account_number: str | None = Field(None, max_length=50)
    website: str | None = Field(None, max_length=200)
    lead_time_days: int | None = Field(None, ge=0, le=365)
    payment_terms: str | None = Field(None, max_length=100)
    notes: str | None = Field(None, max_length=2000)
    status: str | None = None
    is_preferred: bool | None = None

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            return SupplierStatus(str(v).upper()).value
        except ValueError:
            allowed = ", ".join(s.value for s in SupplierStatus)
            raise ValueError(f"Invalid supplier status '{v}'. Allowed: {allowed}")

    @field_validator(
        "contact_name",
        "email",
        "phone",
        "address_line1",
        "address_line2",
        "city",
        "state",
        "postal_code",
        "country",
        "account_number",
        "website",
        "payment_terms",
        "notes",
    )
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        return _clean_optional(v)


class SupplierRead(BaseSchema):
    """A supplier as returned by the API."""

    id: uuid.UUID
    name: str
    contact_name: str | None = None
    email: str | None = None
    phone: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    account_number: str | None = None
    website: str | None = None
    lead_time_days: int
    payment_terms: str | None = None
    notes: str | None = None
    status: str
    is_preferred: bool
    is_active: bool
    address: str
    created_by_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class SupplierSummary(BaseSchema):
    """What the shop has bought from one supplier."""

    supplier_id: uuid.UUID
    supplier_name: str
    status: str
    is_preferred: bool
    total_orders: int
    open_orders: int
    received_orders: int
    total_units_received: float
    total_spend: float
    last_order_date: date | None = None
    last_received_at: datetime | None = None


__all__ = [
    "SupplierCreate",
    "SupplierRead",
    "SupplierSummary",
    "SupplierUpdate",
]
