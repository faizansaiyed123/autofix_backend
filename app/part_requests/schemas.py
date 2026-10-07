"""Pydantic schemas for part requests."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, field_validator

from app.common.schemas import BaseSchema
from app.part_requests.models import PartRequestStatus


def _coerce_enum(value, enum_cls, field_name: str):
    """Validate an incoming string against one of the part-request enums."""
    if value is None:
        return None
    if isinstance(value, enum_cls):
        return value.value
    try:
        return enum_cls(str(value).upper()).value
    except ValueError:
        allowed = ", ".join(m.value for m in enum_cls)
        raise ValueError(f"Invalid {field_name} '{value}'. Allowed: {allowed}")


class PartRequestCreate(BaseSchema):
    """Payload for a technician raising a part request."""

    repair_order_id: uuid.UUID
    repair_task_id: uuid.UUID | None = None
    part_number: str | None = Field(None, max_length=50)
    part_name: str = Field(..., min_length=1, max_length=200)
    quantity: float = Field(1.0, gt=0, le=10_000)
    reason: str = Field(..., min_length=1, max_length=1000)


class PartRequestUpdate(BaseSchema):
    """Partial update of a pending part request."""

    part_number: str | None = Field(None, max_length=50)
    part_name: str | None = Field(None, min_length=1, max_length=200)
    quantity: float | None = Field(None, gt=0, le=10_000)
    reason: str | None = Field(None, min_length=1, max_length=1000)


class PartRequestDecision(BaseSchema):
    """A parts-staff decision on a request (approve or reject)."""

    decision: str
    decision_reason: str | None = Field(None, max_length=1000)

    @field_validator("decision")
    @classmethod
    def _check_decision(cls, v: str) -> str:
        value = _coerce_enum(v, PartRequestStatus, "part request decision")
        if value not in (PartRequestStatus.APPROVED.value, PartRequestStatus.REJECTED.value):
            raise ValueError("decision must be APPROVED or REJECTED")
        return value


class PartRequestRead(BaseSchema):
    """A part request as returned by the API."""

    id: uuid.UUID
    repair_order_id: uuid.UUID
    repair_task_id: uuid.UUID | None = None
    requested_by_id: uuid.UUID | None = None
    decided_by_id: uuid.UUID | None = None
    part_number: str | None = None
    part_name: str
    quantity: float
    reason: str
    status: str
    decision_reason: str | None = None
    decided_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


__all__ = [
    "PartRequestCreate",
    "PartRequestDecision",
    "PartRequestRead",
    "PartRequestUpdate",
]
