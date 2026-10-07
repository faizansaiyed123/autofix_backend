"""Pydantic schemas for service request management."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.common.schemas import BaseSchema
from app.service_requests.models import ServiceRequestPriority


class ServiceRequestBase(BaseSchema):
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(None, max_length=5000)
    priority: str = ServiceRequestPriority.STANDARD.value
    service_advisor_notes: str | None = Field(None, max_length=2000)


class ServiceRequestCreate(ServiceRequestBase):
    customer_id: uuid.UUID
    vehicle_id: uuid.UUID | None = None


class ServiceRequestUpdate(BaseSchema):
    title: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=5000)
    priority: str | None = None
    status: str | None = None
    service_advisor_notes: str | None = Field(None, max_length=2000)
    vehicle_id: uuid.UUID | None = None


class ServiceRequestRead(ServiceRequestBase):
    id: uuid.UUID
    customer_id: uuid.UUID
    vehicle_id: uuid.UUID | None = None
    status: str
    created_at: datetime
    updated_at: datetime
