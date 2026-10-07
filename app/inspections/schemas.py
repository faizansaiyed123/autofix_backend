"""Pydantic schemas for digital vehicle inspections."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, field_validator

from app.common.schemas import BaseSchema
from app.inspections.models import (
    InspectionItemStatus,
    InspectionRecommendation,
    InspectionStatus,
)


def _validate_enum(value: str | None, enum_cls, field_name: str) -> str | None:
    """Coerce and validate an incoming string against an enum."""
    if value is None:
        return None
    try:
        return enum_cls(value.upper()).value
    except ValueError:
        allowed = ", ".join(m.value for m in enum_cls)
        raise ValueError(f"Invalid {field_name} '{value}'. Allowed: {allowed}")


class InspectionPhotoCreate(BaseSchema):
    photo_url: str = Field(..., min_length=1, max_length=500)
    caption: str | None = Field(None, max_length=200)


class InspectionPhotoRead(BaseSchema):
    id: uuid.UUID
    photo_url: str
    caption: str | None = None


class InspectionItemBase(BaseSchema):
    category: str = Field(..., min_length=1, max_length=50)
    item_name: str = Field(..., min_length=1, max_length=100)
    status: str = InspectionItemStatus.GOOD.value
    measurement: str | None = Field(None, max_length=100)
    notes: str | None = Field(None, max_length=2000)
    recommendation: str | None = Field(None, max_length=20)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        return _validate_enum(v, InspectionItemStatus, "inspection item status")

    @field_validator("recommendation")
    @classmethod
    def _check_recommendation(cls, v: str | None) -> str | None:
        return _validate_enum(v, InspectionRecommendation, "recommendation")


class InspectionItemCreate(InspectionItemBase):
    photos: list[InspectionPhotoCreate] = Field(default_factory=list)

    # Convenience shorthand for a single photo.
    photo_url: str | None = Field(None, max_length=500)
    photo_caption: str | None = Field(None, max_length=200)

    def all_photos(self) -> list[InspectionPhotoCreate]:
        """Merge the ``photos`` list with the single-photo shorthand."""
        photos = list(self.photos)
        if self.photo_url:
            photos.append(
                InspectionPhotoCreate(photo_url=self.photo_url, caption=self.photo_caption)
            )
        return photos


class InspectionItemUpdate(BaseSchema):
    category: str | None = Field(None, min_length=1, max_length=50)
    item_name: str | None = Field(None, min_length=1, max_length=100)
    status: str | None = None
    measurement: str | None = Field(None, max_length=100)
    notes: str | None = Field(None, max_length=2000)
    recommendation: str | None = Field(None, max_length=20)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str | None) -> str | None:
        return _validate_enum(v, InspectionItemStatus, "inspection item status")

    @field_validator("recommendation")
    @classmethod
    def _check_recommendation(cls, v: str | None) -> str | None:
        return _validate_enum(v, InspectionRecommendation, "recommendation")


class InspectionItemRead(InspectionItemBase):
    id: uuid.UUID
    severity_color: str
    photos: list[InspectionPhotoRead] = Field(default_factory=list)
    photo_url: str | None = None


class InspectionBase(BaseSchema):
    vehicle_id: uuid.UUID
    customer_id: uuid.UUID
    checkin_id: uuid.UUID | None = None
    technician_id: uuid.UUID | None = None
    mileage: int | None = Field(None, ge=0, le=10_000_000)
    overall_notes: str | None = Field(None, max_length=5000)


class InspectionCreate(InspectionBase):
    items: list[InspectionItemCreate] = Field(default_factory=list)


class InspectionUpdate(BaseSchema):
    status: str | None = None
    technician_id: uuid.UUID | None = None
    mileage: int | None = Field(None, ge=0, le=10_000_000)
    overall_notes: str | None = Field(None, max_length=5000)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str | None) -> str | None:
        return _validate_enum(v, InspectionStatus, "inspection status")


class InspectionRead(InspectionBase):
    id: uuid.UUID
    status: str
    overall_condition: str
    items: list[InspectionItemRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


# --- Customer-facing visual report -----------------------------------------


class ReportItem(BaseSchema):
    """One line of the customer-facing inspection report."""

    item_name: str
    status: str
    severity_color: str
    measurement: str | None = None
    recommendation: str | None = None
    notes: str | None = None
    photos: list[InspectionPhotoRead] = Field(default_factory=list)


class ReportCategory(BaseSchema):
    """A named group of report items (Brakes, Tires, Under Hood, ...)."""

    category: str
    severity_color: str
    items: list[ReportItem]


class ReportSummary(BaseSchema):
    """Traffic-light counts across the whole inspection."""

    total_items: int
    green: int
    yellow: int
    red: int
    not_checked: int


class InspectionReport(BaseSchema):
    """Customer-friendly inspection report (GREEN / YELLOW / RED)."""

    inspection_id: uuid.UUID
    status: str
    vehicle_id: uuid.UUID
    customer_id: uuid.UUID
    mileage: int | None = None
    overall_condition: str
    summary: ReportSummary
    categories: list[ReportCategory]
    urgent_items: list[ReportItem]
    recommended_items: list[ReportItem]
    overall_notes: str | None = None
    inspected_at: datetime
