"""Pydantic schemas for notifications."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.common.schemas import BaseSchema


class NotificationRead(BaseSchema):
    """A notification as the client sees it."""

    id: uuid.UUID
    notification_type: str
    title: str
    body: str | None = None
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    priority: str
    # Derived from read_at rather than stored beside it — see the model.
    is_read: bool
    read_at: datetime | None = None
    created_at: datetime


class NotificationSummary(BaseSchema):
    """The badge, for a header that polls.

    Cheap to compute and deliberately tiny: this is fetched on every page load,
    and it should never be the reason a page feels slow.
    """

    unread_count: int = 0
    high_priority_unread: int = 0
    latest_unread_at: datetime | None = None


class NotificationList(BaseSchema):
    """A page of notifications with the counts alongside.

    The list and the badge travel together so the client can render "3 unread"
    above the items without a second request, and so the two can never disagree.
    """

    notifications: list[NotificationRead] = Field(default_factory=list)
    total: int = 0
    unread_count: int = 0


__all__ = [
    "NotificationList",
    "NotificationRead",
    "NotificationSummary",
]
