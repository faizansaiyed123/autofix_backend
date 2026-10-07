"""Pydantic schemas for repair orders and tasks."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, field_validator

from app.common.schemas import BaseSchema
from app.repair_orders.models import RepairOrderStatus, RepairTaskStatus


def _coerce_enum(value, enum_cls, field_name: str):
    """Validate an incoming string against one of the repair-order enums."""
    if value is None:
        return None
    if isinstance(value, enum_cls):
        return value.value
    try:
        return enum_cls(str(value).upper()).value
    except ValueError:
        allowed = ", ".join(m.value for m in enum_cls)
        raise ValueError(f"Invalid {field_name} '{value}'. Allowed: {allowed}")


class RepairTaskBase(BaseSchema):
    """Fields shared by task create and update payloads."""

    description: str = Field(..., min_length=1, max_length=300)
    notes: str | None = Field(None, max_length=1000)


class RepairTaskCreate(RepairTaskBase):
    """Payload for adding a task to a repair order."""

    assigned_to_id: uuid.UUID | None = None
    sequence: int | None = Field(None, ge=0, le=10_000)


class RepairTaskUpdate(BaseSchema):
    """Partial update of an existing task."""

    description: str | None = Field(None, min_length=1, max_length=300)
    notes: str | None = Field(None, max_length=1000)
    assigned_to_id: uuid.UUID | None = None
    sequence: int | None = Field(None, ge=0, le=10_000)


class RepairTaskStatusUpdate(BaseSchema):
    """Explicit task status transition."""

    status: str
    notes: str | None = Field(None, max_length=1000)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        return _coerce_enum(v, RepairTaskStatus, "task status")


class RepairTaskRead(RepairTaskBase):
    """A task as returned by the API."""

    id: uuid.UUID
    repair_order_id: uuid.UUID
    status: str
    sequence: int
    assigned_to_id: uuid.UUID | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class RepairOrderBase(BaseSchema):
    """Fields shared by repair-order create and update payloads."""

    advisor_id: uuid.UUID | None = None
    technician_id: uuid.UUID | None = None
    appointment_id: uuid.UUID | None = None
    odometer_in: int | None = Field(None, ge=0, le=2_000_000)
    odometer_out: int | None = Field(None, ge=0, le=2_000_000)
    bay: str | None = Field(None, max_length=30)
    promised_at: datetime | None = None
    notes: str | None = Field(None, max_length=5000)
    customer_notes: str | None = Field(None, max_length=5000)


class RepairOrderCreate(RepairOrderBase):
    """Payload for creating a repair order with optional tasks."""

    customer_id: uuid.UUID
    vehicle_id: uuid.UUID
    estimate_id: uuid.UUID | None = None
    tasks: list[RepairTaskCreate] = Field(default_factory=list)


class RepairOrderUpdate(BaseSchema):
    """Partial update of a repair order's header."""

    advisor_id: uuid.UUID | None = None
    technician_id: uuid.UUID | None = None
    odometer_in: int | None = Field(None, ge=0, le=2_000_000)
    odometer_out: int | None = Field(None, ge=0, le=2_000_000)
    bay: str | None = Field(None, max_length=30)
    promised_at: datetime | None = None
    notes: str | None = Field(None, max_length=5000)
    customer_notes: str | None = Field(None, max_length=5000)


class RepairOrderStatusUpdate(BaseSchema):
    """Explicit repair-order status transition."""

    status: str
    reason: str | None = Field(None, max_length=1000)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        return _coerce_enum(v, RepairOrderStatus, "repair order status")


class RepairOrderRead(RepairOrderBase):
    """A repair order as returned by the API."""

    id: uuid.UUID
    ro_number: str
    customer_id: uuid.UUID
    vehicle_id: uuid.UUID
    estimate_id: uuid.UUID | None = None
    status: str
    cancel_reason: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    delivered_at: datetime | None = None
    all_tasks_done: bool
    tasks: list[RepairTaskRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class RepairOrderTaskCounts(BaseSchema):
    """How many tasks sit in each execution state."""

    total: int
    pending: int
    in_progress: int
    completed: int
    skipped: int


class RepairOrderSummary(BaseSchema):
    """Compact operational view of a repair order."""

    repair_order_id: uuid.UUID
    ro_number: str
    status: str
    is_terminal: bool
    tasks_editable: bool
    all_tasks_done: bool
    counts: RepairOrderTaskCounts


__all__ = [
    "RepairOrderBase",
    "RepairOrderCreate",
    "RepairOrderRead",
    "RepairOrderStatusUpdate",
    "RepairOrderSummary",
    "RepairOrderTaskCounts",
    "RepairOrderUpdate",
    "RepairTaskBase",
    "RepairTaskCreate",
    "RepairTaskRead",
    "RepairTaskStatusUpdate",
    "RepairTaskUpdate",
]
