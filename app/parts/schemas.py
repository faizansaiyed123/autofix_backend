"""Pydantic schemas for the parts catalog."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, field_validator

from app.common.schemas import BaseSchema
from app.parts.models import PartStatus


def _upper(value: str | None) -> str | None:
    """Normalise a catalog code so 'bos0986a' and 'BOS0986A' are one part."""
    return value.strip().upper() if value is not None else None


class PartCreate(BaseSchema):
    """Payload for adding a catalog line.

    ``quantity_on_hand`` is deliberately not accepted: a new part starts empty
    and is stocked through a RECEIPT transaction, so the opening balance is on
    the ledger like everything after it.
    """

    part_number: str = Field(..., min_length=1, max_length=50)
    sku: str | None = Field(None, max_length=50)
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    category: str = Field(..., min_length=1, max_length=50)
    brand: str | None = Field(None, max_length=100)
    location: str | None = Field(None, max_length=50)
    unit_cost: float = Field(0.0, ge=0, le=1_000_000)
    unit_price: float = Field(0.0, ge=0, le=1_000_000)
    reorder_level: float = Field(0.0, ge=0, le=1_000_000)

    @field_validator("part_number", "sku")
    @classmethod
    def _normalise_code(cls, v: str | None) -> str | None:
        return _upper(v)


class PartUpdate(BaseSchema):
    """Partial update of a catalog line.

    Pricing and reorder levels are editable; identity (``part_number``, ``sku``)
    and the stock balance are not. Renumbering a part would orphan the history
    already filed against the old number, and the balance moves only through
    inventory transactions.
    """

    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    category: str | None = Field(None, min_length=1, max_length=50)
    brand: str | None = Field(None, max_length=100)
    location: str | None = Field(None, max_length=50)
    unit_cost: float | None = Field(None, ge=0, le=1_000_000)
    unit_price: float | None = Field(None, ge=0, le=1_000_000)
    reorder_level: float | None = Field(None, ge=0, le=1_000_000)
    status: str | None = None

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            return PartStatus(str(v).upper()).value
        except ValueError:
            allowed = ", ".join(s.value for s in PartStatus)
            raise ValueError(f"Invalid part status '{v}'. Allowed: {allowed}")


class PartRead(BaseSchema):
    """A catalog line as returned by the API."""

    id: uuid.UUID
    part_number: str
    sku: str | None = None
    name: str
    description: str | None = None
    category: str
    brand: str | None = None
    location: str | None = None
    unit_cost: float
    unit_price: float
    quantity_on_hand: float
    reorder_level: float
    status: str
    margin: float
    stock_value: float
    stock_status: str
    is_low_stock: bool
    is_out_of_stock: bool
    created_at: datetime
    updated_at: datetime


__all__ = [
    "PartCreate",
    "PartRead",
    "PartUpdate",
]
