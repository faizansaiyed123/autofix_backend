"""Pydantic schemas for estimates and customer approval."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import Field, field_validator, model_validator

from app.common.schemas import BaseSchema
from app.estimates.models import (
    EstimateItemStatus,
    EstimateItemType,
    EstimateStatus,
)


def _coerce_enum(value, enum_cls, field_name: str):
    """Validate an incoming string against one of the estimate enums."""
    if value is None:
        return None
    if isinstance(value, enum_cls):
        return value.value
    try:
        return enum_cls(str(value).upper()).value
    except ValueError:
        allowed = ", ".join(m.value for m in enum_cls)
        raise ValueError(f"Invalid {field_name} '{value}'. Allowed: {allowed}")


class EstimateItemBase(BaseSchema):
    """Fields shared by estimate item create and update payloads."""

    item_type: str = EstimateItemType.LABOR.value
    description: str = Field(..., min_length=1, max_length=300)
    labor_hours: float | None = Field(None, ge=0, le=1000)
    labor_rate: float | None = Field(None, ge=0, le=10_000)
    part_number: str | None = Field(None, max_length=50)
    part_name: str | None = Field(None, max_length=200)
    quantity: float = Field(1.0, gt=0, le=10_000)
    unit_price: float = Field(0.0, ge=0, le=1_000_000)
    discount_amount: float = Field(0.0, ge=0, le=1_000_000)
    is_optional: bool = False
    notes: str | None = Field(None, max_length=1000)

    @field_validator("item_type")
    @classmethod
    def _check_item_type(cls, v: str) -> str:
        return _coerce_enum(v, EstimateItemType, "estimate item type")


class EstimateItemCreate(EstimateItemBase):
    """Payload for adding a priced line to a new or draft estimate."""

    @model_validator(mode="after")
    def _check_type_requirements(self) -> EstimateItemCreate:
        _validate_line(self.item_type, self.labor_hours, self.labor_rate, self.unit_price)
        return self


class EstimateItemUpdate(BaseSchema):
    """Partial update of an existing estimate line.

    The item type is intentionally immutable: changing it would silently
    re-interpret already-calculated amounts, so a line is replaced instead.
    """

    description: str | None = Field(None, min_length=1, max_length=300)
    labor_hours: float | None = Field(None, ge=0, le=1000)
    labor_rate: float | None = Field(None, ge=0, le=10_000)
    part_number: str | None = Field(None, max_length=50)
    part_name: str | None = Field(None, max_length=200)
    quantity: float | None = Field(None, gt=0, le=10_000)
    unit_price: float | None = Field(None, ge=0, le=1_000_000)
    discount_amount: float | None = Field(None, ge=0, le=1_000_000)
    is_optional: bool | None = None
    notes: str | None = Field(None, max_length=1000)
    sequence: int | None = Field(None, ge=0, le=10_000)


def _validate_line(item_type, labor_hours, labor_rate, unit_price) -> None:
    """Enforce the per-type requirements of a priced line.

    Raises ``ValueError`` for a line missing the inputs its type needs, which
    Pydantic surfaces as a 422.
    """
    if item_type == EstimateItemType.LABOR.value:
        if labor_hours is None or labor_rate is None:
            raise ValueError("LABOR lines require both labor_hours and labor_rate")
    elif item_type == EstimateItemType.PART.value:
        if not unit_price:
            raise ValueError("PART lines require a unit_price greater than zero")
    elif item_type == EstimateItemType.DISCOUNT.value and not unit_price:
        raise ValueError("DISCOUNT lines require a unit_price greater than zero")


class EstimateItemRead(EstimateItemBase):
    """A priced line as returned by the API."""

    id: uuid.UUID
    status: str
    line_total: float
    customer_notes: str | None = None
    created_at: datetime
    updated_at: datetime


class EstimateBase(BaseSchema):
    """Fields shared by estimate create and update payloads."""

    valid_until: date | None = None
    tax_rate: float = Field(0.0, ge=0, le=1)
    notes: str | None = Field(None, max_length=5000)


class EstimateCreate(EstimateBase):
    """Payload for creating an estimate with optional lines."""

    customer_id: uuid.UUID
    vehicle_id: uuid.UUID
    inspection_id: uuid.UUID | None = None
    service_request_id: uuid.UUID | None = None
    items: list[EstimateItemCreate] = Field(default_factory=list)


class EstimateUpdate(BaseSchema):
    """Partial update of a draft estimate's header."""

    valid_until: date | None = None
    tax_rate: float | None = Field(None, ge=0, le=1)
    notes: str | None = Field(None, max_length=5000)
    customer_notes: str | None = Field(None, max_length=5000)


class EstimateRead(EstimateBase):
    """An estimate as returned by the API."""

    id: uuid.UUID
    estimate_number: str
    customer_id: uuid.UUID
    vehicle_id: uuid.UUID
    inspection_id: uuid.UUID | None = None
    service_request_id: uuid.UUID | None = None
    created_by_id: uuid.UUID | None = None
    status: str
    subtotal: float
    discount_amount: float
    tax_amount: float
    total: float
    customer_notes: str | None = None
    decline_reason: str | None = None
    is_expired: bool
    approved_total: float
    sent_at: datetime | None = None
    decided_at: datetime | None = None
    items: list[EstimateItemRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ItemDecision(BaseSchema):
    """A customer's decision on one estimate line."""

    decision: str
    notes: str | None = Field(None, max_length=1000)

    @field_validator("decision")
    @classmethod
    def _check_decision(cls, v: str) -> str:
        value = _coerce_enum(v, EstimateItemStatus, "item decision")
        if value == EstimateItemStatus.PENDING.value:
            raise ValueError("A decision must be APPROVED or DECLINED")
        return value

    @property
    def is_approval(self) -> bool:
        return self.decision == EstimateItemStatus.APPROVED.value


class EstimateTotals(BaseSchema):
    """Money breakdown of an estimate, split by customer decision."""

    subtotal: float
    discount_amount: float
    tax_rate: float
    tax_amount: float
    total: float
    approved_total: float


class EstimateItemCounts(BaseSchema):
    """How many lines sit in each decision state."""

    total: int
    pending: int
    approved: int
    declined: int


class EstimateSummary(BaseSchema):
    """Customer-facing money summary for an estimate."""

    estimate_id: uuid.UUID
    estimate_number: str
    status: str
    is_expired: bool
    can_decide: bool
    valid_until: date | None = None
    totals: EstimateTotals
    counts: EstimateItemCounts


class EstimateStatusChange(BaseSchema):
    """Explicit estimate status transition (advisor only)."""

    status: str
    reason: str | None = Field(None, max_length=1000)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        return _coerce_enum(v, EstimateStatus, "estimate status")


__all__ = [
    "EstimateBase",
    "EstimateCreate",
    "EstimateItemBase",
    "EstimateItemCounts",
    "EstimateItemCreate",
    "EstimateItemRead",
    "EstimateItemUpdate",
    "EstimateRead",
    "EstimateStatusChange",
    "EstimateSummary",
    "EstimateTotals",
    "ItemDecision",
]
