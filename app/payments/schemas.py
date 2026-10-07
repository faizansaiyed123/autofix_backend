"""Pydantic schemas for payments."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import Field, field_validator, model_validator

from app.common.schemas import BaseSchema
from app.payments.models import (
    REFERENCED_PAYMENT_METHODS,
    PaymentMethod,
    PaymentStatus,
)


def _coerce_enum(value, enum_cls, field_name: str):
    """Validate an incoming string against one of the payment enums."""
    if value is None:
        return None
    if isinstance(value, enum_cls):
        return value.value
    try:
        return enum_cls(str(value).upper()).value
    except ValueError:
        allowed = ", ".join(m.value for m in enum_cls)
        raise ValueError(f"Invalid {field_name} '{value}'. Allowed: {allowed}")


class PaymentCreate(BaseSchema):
    """Payload for recording money against an invoice.

    ``amount`` may be less than the invoice's balance: a bill can be settled over
    several visits, and the remainder simply stays owed.
    """

    invoice_id: uuid.UUID
    amount: float = Field(..., gt=0, le=10_000_000)
    method: str = PaymentMethod.CASH.value
    payment_date: date | None = None
    reference: str | None = Field(None, max_length=100)
    notes: str | None = Field(None, max_length=1000)

    @field_validator("method")
    @classmethod
    def _check_method(cls, v: str) -> str:
        return _coerce_enum(v, PaymentMethod, "payment method")

    @field_validator("reference")
    @classmethod
    def _trim_reference(cls, v: str | None) -> str | None:
        return v.strip() if v else v

    @model_validator(mode="after")
    def _check_reference_present(self) -> PaymentCreate:
        """A transfer or a cheque with no reference cannot be reconciled.

        Checked on the model rather than on the field because the rule is about
        the pair: a cash payment needs no slip, a bank transfer always does. A
        field validator would also never see it — Pydantic does not run those on
        a field the caller left at its default, which is exactly the case this
        rule is about.
        """
        if self.method in REFERENCED_PAYMENT_METHODS and not (self.reference or "").strip():
            raise ValueError(
                f"a {self.method} payment requires a reference so it can be matched to "
                "the bank statement"
            )
        return self


class PaymentVoid(BaseSchema):
    """Reason for reversing a recorded payment."""

    reason: str = Field(..., min_length=1, max_length=1000)


class PaymentRead(BaseSchema):
    """A payment as returned by the API."""

    id: uuid.UUID
    invoice_id: uuid.UUID
    amount: float
    method: str
    status: str
    payment_date: date
    reference: str | None = None
    notes: str | None = None
    recorded_by_id: uuid.UUID | None = None
    void_reason: str | None = None
    voided_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class PaymentTotals(BaseSchema):
    """Money in, money reversed, and what is left after both."""

    total_received: float
    total_voided: float
    net_received: float
    payment_count: int
    void_count: int
    by_method: dict[str, float] = Field(default_factory=dict)


class PaymentSummary(BaseSchema):
    """Shop-level takings over a period, for the front desk and reports."""

    start_date: date | None = None
    end_date: date | None = None
    totals: PaymentTotals


__all__ = [
    "PaymentCreate",
    "PaymentMethod",
    "PaymentRead",
    "PaymentStatus",
    "PaymentSummary",
    "PaymentTotals",
    "PaymentVoid",
]
