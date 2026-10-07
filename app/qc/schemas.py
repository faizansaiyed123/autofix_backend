"""Pydantic schemas for quality control."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.common.schemas import BaseSchema
from app.qc.models import QCCheckType, QualityCheckStatus


def _coerce_enum(value, enum_cls, field_name: str):
    """Validate an incoming string against one of the QC enums."""
    if value is None:
        return None
    if isinstance(value, enum_cls):
        return value.value
    try:
        return enum_cls(str(value).upper()).value
    except ValueError:
        allowed = ", ".join(m.value for m in enum_cls)
        raise ValueError(f"Invalid {field_name} '{value}'. Allowed: {allowed}")


class QualityCheckCreate(BaseSchema):
    """Payload for starting a QC attempt on a completed repair order."""

    repair_order_id: uuid.UUID
    notes: str | None = Field(None, max_length=5000)


class QualityCheckUpdate(BaseSchema):
    """Partial update of an in-progress QC attempt."""

    notes: str | None = Field(None, max_length=5000)


class QualityCheckDecision(BaseSchema):
    """Verdict on a QC attempt."""

    reason: str | None = Field(None, max_length=5000)


class QCCheckItemOverride(BaseSchema):
    """An inspector's manual verdict on a single verification check."""

    passed: bool
    blocking: bool | None = None
    notes: str | None = Field(None, max_length=1000)


class QCPhotoCreate(BaseSchema):
    """Payload for attaching a photo to a QC attempt."""

    photo_url: str = Field(..., min_length=1, max_length=500)
    caption: str | None = Field(None, max_length=200)


class QCCheckItemRead(BaseSchema):
    """A verification check line as returned by the API."""

    id: uuid.UUID
    quality_check_id: uuid.UUID
    check_type: str
    passed: bool
    blocking: bool
    auto_verified: bool
    evidence: str | None = None
    notes: str | None = None


class QCPhotoRead(BaseSchema):
    """A QC photo as returned by the API."""

    id: uuid.UUID
    quality_check_id: uuid.UUID
    photo_url: str
    caption: str | None = None
    created_at: datetime


class QualityCheckRead(BaseSchema):
    """A QC attempt as returned by the API."""

    id: uuid.UUID
    repair_order_id: uuid.UUID
    inspector_id: uuid.UUID | None = None
    status: str
    attempt_number: int
    started_at: datetime | None = None
    completed_at: datetime | None = None
    notes: str | None = None
    failure_reason: str | None = None
    is_terminal: bool
    passed: bool
    checks: list[QCCheckItemRead] = Field(default_factory=list)
    photos: list[QCPhotoRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class QCQueueItem(BaseSchema):
    """A completed repair order waiting on quality control."""

    repair_order_id: uuid.UUID
    ro_number: str
    customer_id: uuid.UUID
    vehicle_id: uuid.UUID
    technician_id: uuid.UUID | None = None
    completed_at: datetime | None = None
    has_open_check: bool


__all__ = [
    "QCCheckItemOverride",
    "QCCheckItemRead",
    "QCCheckType",
    "QCPhotoCreate",
    "QCPhotoRead",
    "QCQueueItem",
    "QualityCheckCreate",
    "QualityCheckDecision",
    "QualityCheckRead",
    "QualityCheckStatus",
    "QualityCheckUpdate",
]
