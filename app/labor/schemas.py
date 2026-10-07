"""Pydantic schemas for labor records and the technician dashboard."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, model_validator

from app.common.schemas import BaseSchema


class LaborRecordBase(BaseSchema):
    """Fields shared by labor create and update payloads."""

    description: str = Field(..., min_length=1, max_length=300)
    actual_hours: float = Field(..., gt=0, le=100)
    billable_hours: float | None = Field(None, ge=0, le=100)
    hourly_rate: float = Field(0.0, ge=0, le=10_000)
    notes: str | None = Field(None, max_length=1000)


class LaborRecordCreate(LaborRecordBase):
    """Payload for logging labor against a repair order."""

    repair_order_id: uuid.UUID
    repair_task_id: uuid.UUID | None = None
    technician_id: uuid.UUID | None = None
    performed_at: datetime | None = None

    @model_validator(mode="after")
    def _default_billable_to_actual(self) -> LaborRecordCreate:
        # Billable hours default to actual hours; an explicit value always wins.
        if self.billable_hours is None:
            self.billable_hours = self.actual_hours
        return self


class LaborRecordUpdate(BaseSchema):
    """Partial update of an existing labor record."""

    description: str | None = Field(None, min_length=1, max_length=300)
    actual_hours: float | None = Field(None, gt=0, le=100)
    billable_hours: float | None = Field(None, ge=0, le=100)
    hourly_rate: float | None = Field(None, ge=0, le=10_000)
    technician_id: uuid.UUID | None = None
    performed_at: datetime | None = None
    notes: str | None = Field(None, max_length=1000)


class LaborRecordRead(LaborRecordBase):
    """A labor record as returned by the API."""

    id: uuid.UUID
    repair_order_id: uuid.UUID
    repair_task_id: uuid.UUID | None = None
    technician_id: uuid.UUID | None = None
    billable_hours: float
    performed_at: datetime
    labor_cost: float
    created_at: datetime
    updated_at: datetime


class TechnicianDashboardOpenTask(BaseSchema):
    """An open task on a repair order assigned to the technician."""

    task_id: uuid.UUID
    description: str
    repair_order_id: uuid.UUID
    ro_number: str
    status: str


class TechnicianDashboard(BaseSchema):
    """Aggregate view of a technician's current workload."""

    technician_id: uuid.UUID
    open_task_count: int
    in_progress_ro_count: int
    hours_this_week: float
    pending_part_request_count: int
    open_tasks: list[TechnicianDashboardOpenTask] = Field(default_factory=list)


__all__ = [
    "LaborRecordBase",
    "LaborRecordCreate",
    "LaborRecordRead",
    "LaborRecordUpdate",
    "TechnicianDashboard",
    "TechnicianDashboardOpenTask",
]
