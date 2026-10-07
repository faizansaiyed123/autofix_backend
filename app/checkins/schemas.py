"""Pydantic schemas for check-in management."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.checkins.models import CheckInType
from app.common.schemas import BaseSchema


class CheckInBase(BaseSchema):
    vehicle_id: uuid.UUID
    customer_id: uuid.UUID
    odometer: int = Field(..., gt=0, description="Current odometer reading")
    checkin_type: str = CheckInType.DRIVE_IN.value
    notes: str | None = Field(None, max_length=2000)
    expected_completion: str | None = None
    tire_condition: str | None = Field(None, max_length=50)
    fluid_levels: str | None = Field(None, max_length=50)
    lights_status: str | None = Field(None, max_length=50)


class CheckInCreate(CheckInBase):
    service_advisor_id: uuid.UUID | None = None


class CheckInUpdate(BaseSchema):
    status: str | None = None
    odometer: int | None = Field(None, gt=0)
    notes: str | None = Field(None, max_length=2000)
    expected_completion: str | None = None
    tire_condition: str | None = Field(None, max_length=50)
    fluid_levels: str | None = Field(None, max_length=50)
    lights_status: str | None = Field(None, max_length=50)
    service_advisor_id: uuid.UUID | None = None


class CheckInRead(CheckInBase):
    id: uuid.UUID
    status: str
    service_advisor_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
