"""Pydantic schemas for invoicing."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import Field, field_validator, model_validator

from app.common.schemas import BaseSchema
from app.invoices.models import (
    InvoiceItemSource,
    InvoiceItemType,
    InvoiceStatus,
)


def _coerce_enum(value, enum_cls, field_name: str):
    """Validate an incoming string against one of the invoice enums."""
    if value is None:
        return None
    if isinstance(value, enum_cls):
        return value.value
    try:
        return enum_cls(str(value).upper()).value
    except ValueError:
        allowed = ", ".join(m.value for m in enum_cls)
        raise ValueError(f"Invalid {field_name} '{value}'. Allowed: {allowed}")


def _validate_line(item_type: str, quantity, unit_price, discount_amount=0.0) -> None:
    """Enforce the per-type requirements of a charge line.

    Raises ``ValueError`` for a line missing the inputs its type needs, which
    Pydantic surfaces as a 422.
    """
    if quantity is not None and float(quantity) <= 0:
        raise ValueError("quantity must be greater than zero")
    if item_type == InvoiceItemType.DISCOUNT.value:
        # A discount is a single amount, not a rate: "10% off" is quoted as the
        # money it saves, so the bill can be re-read without re-deriving it.
        if not unit_price:
            raise ValueError("DISCOUNT lines require a unit_price greater than zero")
        if float(quantity) != 1.0:
            raise ValueError("DISCOUNT lines must have a quantity of 1")
        if discount_amount:
            raise ValueError("DISCOUNT lines cannot also carry a discount_amount")
        return
    if float(unit_price or 0.0) <= 0:
        raise ValueError(f"{item_type} lines require a unit_price greater than zero")


class InvoiceItemBase(BaseSchema):
    """Fields shared by invoice item create and update payloads."""

    item_type: str = InvoiceItemType.PART.value
    description: str = Field(..., min_length=1, max_length=300)
    quantity: float = Field(1.0, gt=0, le=10_000)
    unit_price: float = Field(..., gt=0, le=1_000_000)
    discount_amount: float = Field(0.0, ge=0, le=1_000_000)
    part_number: str | None = Field(None, max_length=50)
    part_name: str | None = Field(None, max_length=200)
    notes: str | None = Field(None, max_length=1000)

    @field_validator("item_type")
    @classmethod
    def _check_item_type(cls, v: str) -> str:
        return _coerce_enum(v, InvoiceItemType, "invoice item type")


class InvoiceItemCreate(InvoiceItemBase):
    """Payload for adding a charge line to a draft invoice."""

    @model_validator(mode="after")
    def _check_line(self) -> InvoiceItemCreate:
        _validate_line(self.item_type, self.quantity, self.unit_price, self.discount_amount)
        return self


class InvoiceItemUpdate(BaseSchema):
    """Partial update of a shop-added invoice line.

    The item type is immutable: re-interpreting a line would silently change what
    the customer was charged, so a line is replaced instead.
    """

    description: str | None = Field(None, min_length=1, max_length=300)
    quantity: float | None = Field(None, gt=0, le=10_000)
    unit_price: float | None = Field(None, gt=0, le=1_000_000)
    discount_amount: float | None = Field(None, ge=0, le=1_000_000)
    part_number: str | None = Field(None, max_length=50)
    part_name: str | None = Field(None, max_length=200)
    notes: str | None = Field(None, max_length=1000)
    sequence: int | None = Field(None, ge=0, le=10_000)


class InvoiceItemRead(InvoiceItemBase):
    """A charge line as returned by the API."""

    id: uuid.UUID
    source: str
    line_total: float
    estimate_item_id: uuid.UUID | None = None
    reference: str | None = None
    created_at: datetime
    updated_at: datetime


class InvoiceBase(BaseSchema):
    """Fields shared by invoice create and update payloads."""

    invoice_date: date | None = None
    due_date: date | None = None
    tax_rate: float | None = Field(None, ge=0, le=1)
    notes: str | None = Field(None, max_length=5000)
    customer_notes: str | None = Field(None, max_length=5000)


class InvoiceCreate(InvoiceBase):
    """Payload for raising an invoice against a repair order.

    ``extra_items`` are the shop's own additions (extra work agreed at the
    counter). The approved estimate lines are copied onto the invoice
    automatically, so they are never asked for here.
    """

    repair_order_id: uuid.UUID
    extra_items: list[InvoiceItemCreate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_dates(self) -> InvoiceCreate:
        if self.invoice_date and self.due_date and self.due_date < self.invoice_date:
            raise ValueError("due_date cannot be earlier than invoice_date")
        return self


class InvoiceUpdate(BaseSchema):
    """Partial update of a draft invoice's header."""

    invoice_date: date | None = None
    due_date: date | None = None
    tax_rate: float | None = Field(None, ge=0, le=1)
    notes: str | None = Field(None, max_length=5000)
    customer_notes: str | None = Field(None, max_length=5000)

    @model_validator(mode="after")
    def _check_dates(self) -> InvoiceUpdate:
        if self.invoice_date and self.due_date and self.due_date < self.invoice_date:
            raise ValueError("due_date cannot be earlier than invoice_date")
        return self


class InvoiceRead(InvoiceBase):
    """An invoice as returned by the API."""

    id: uuid.UUID
    invoice_number: str
    customer_id: uuid.UUID
    vehicle_id: uuid.UUID
    repair_order_id: uuid.UUID
    estimate_id: uuid.UUID | None = None
    created_by_id: uuid.UUID | None = None
    status: str
    subtotal: float
    discount_amount: float
    tax_amount: float
    total: float
    amount_paid: float
    balance: float
    is_overdue: bool
    days_overdue: int
    void_reason: str | None = None
    issued_at: datetime | None = None
    paid_at: datetime | None = None
    voided_at: datetime | None = None
    items: list[InvoiceItemRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class InvoiceTotals(BaseSchema):
    """Money breakdown of an invoice."""

    subtotal: float
    discount_amount: float
    tax_rate: float
    tax_amount: float
    total: float
    amount_paid: float
    balance: float


class InvoiceSummary(BaseSchema):
    """Customer-facing money summary for an invoice."""

    invoice_id: uuid.UUID
    invoice_number: str
    status: str
    invoice_date: date
    due_date: date | None = None
    is_overdue: bool
    days_overdue: int
    item_count: int
    totals: InvoiceTotals


class InvoiceStatusChange(BaseSchema):
    """Explicit invoice status transition (advisor only)."""

    status: str
    reason: str | None = Field(None, max_length=1000)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        return _coerce_enum(v, InvoiceStatus, "invoice status")


__all__ = [
    "InvoiceBase",
    "InvoiceCreate",
    "InvoiceItemBase",
    "InvoiceItemCreate",
    "InvoiceItemRead",
    "InvoiceItemSource",
    "InvoiceItemType",
    "InvoiceItemUpdate",
    "InvoiceRead",
    "InvoiceStatus",
    "InvoiceStatusChange",
    "InvoiceSummary",
    "InvoiceTotals",
]
