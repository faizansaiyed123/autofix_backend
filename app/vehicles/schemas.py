"""Pydantic schemas for vehicle management."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.common.schemas import BaseSchema
from app.vehicles.models import MileageSource, VehicleStatus


class MileageRecordBase(BaseSchema):
    mileage: int = Field(..., gt=0, description="Odometer reading in miles or km")
    source: str = Field(default=MileageSource.MANUAL.value)
    notes: str | None = Field(None, max_length=500)


class MileageRecordCreate(MileageRecordBase):
    pass


class MileageRecordRead(MileageRecordBase):
    id: uuid.UUID
    vehicle_id: uuid.UUID
    created_at: datetime


class VehicleBase(BaseSchema):
    vin: str | None = Field(None, min_length=11, max_length=17, description="17-character VIN")
    license_plate: str | None = Field(None, max_length=20)
    make: str = Field(..., min_length=1, max_length=50)
    model: str = Field(..., min_length=1, max_length=100)
    year: int | None = Field(None, ge=1900, le=2030)
    trim: str | None = Field(None, max_length=100)
    engine: str | None = Field(None, max_length=100)
    transmission: str | None = Field(None, max_length=50)
    mileage: int | None = Field(None, ge=0)
    color: str | None = Field(None, max_length=30)
    fuel_type: str | None = Field(None, max_length=20)
    purchase_date: str | None = None
    notes: str | None = Field(None, max_length=2000)


class VehicleCreate(VehicleBase):
    customer_id: uuid.UUID
    status: VehicleStatus = VehicleStatus.ACTIVE


class VehicleUpdate(BaseSchema):
    vin: str | None = Field(None, min_length=11, max_length=17)
    license_plate: str | None = Field(None, max_length=20)
    make: str | None = Field(None, max_length=50)
    model: str | None = Field(None, max_length=100)
    year: int | None = Field(None, ge=1900, le=2030)
    trim: str | None = Field(None, max_length=100)
    engine: str | None = Field(None, max_length=100)
    transmission: str | None = Field(None, max_length=50)
    mileage: int | None = Field(None, ge=0)
    color: str | None = Field(None, max_length=30)
    fuel_type: str | None = Field(None, max_length=20)
    purchase_date: str | None = None
    notes: str | None = Field(None, max_length=2000)
    status: VehicleStatus | None = None


class VehicleRead(VehicleBase):
    id: uuid.UUID
    customer_id: uuid.UUID
    status: str
    mileage_records: list[MileageRecordRead] = Field(default_factory=list)

    @property
    def full_name(self) -> str:
        parts = [str(self.year) if self.year else "", self.make, self.model]
        return " ".join(p for p in parts if p)
