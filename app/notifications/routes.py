"""API routes for the notification centre.

Every route is scoped to the **signed-in user**. There is no ``recipient_id``
parameter anywhere in this file, and that is the point: a notification centre
that accepted a user id would be a way to read somebody else's inbox. A
notification belonging to somebody else is reported as not found, not forbidden —
a 403 would confirm the id exists.

Endpoints:
- GET   /                     The user's notifications, unread first
- GET   /summary              The badge: unread and high-priority counts
- GET   /unread-count         Just the badge, for a polling header
- GET   /{notification_id}    One notification
- POST  /{notification_id}/read
- POST  /{notification_id}/unread
- POST  /mark-all-read
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_permission
from app.auth.models import User
from app.auth.permissions import PermissionEnum
from app.core.database import get_session
from app.notifications.schemas import (
    NotificationList,
    NotificationRead,
    NotificationSummary,
)
from app.notifications.services import NotificationService

router = APIRouter()


def get_notification_service(
    db: AsyncSession = Depends(get_session),
) -> NotificationService:
    return NotificationService(db)


@router.get("/", response_model=NotificationList)
async def list_notifications(
    unread_only: bool = Query(False, description="Only notifications not yet read"),
    notification_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(require_permission(PermissionEnum.NOTIFICATIONS_READ)),
    service: NotificationService = Depends(get_notification_service),
):
    """The signed-in user's notifications, unread first and then newest first."""
    notifications = await service.list_for_user(
        current_user.id,
        unread_only=unread_only,
        notification_type=notification_type,
        limit=limit,
        offset=offset,
    )
    return NotificationList(
        notifications=[NotificationRead.model_validate(n) for n in notifications],
        total=await service.count_for_user(current_user.id),
        unread_count=await service.unread_count(current_user.id),
    )


@router.get("/summary", response_model=NotificationSummary)
async def get_summary(
    current_user: User = Depends(require_permission(PermissionEnum.NOTIFICATIONS_READ)),
    service: NotificationService = Depends(get_notification_service),
):
    """The badge, plus when the most recent unread arrived."""
    return NotificationSummary(
        unread_count=await service.unread_count(current_user.id),
        high_priority_unread=await service.count_high_priority_unread(current_user.id),
        latest_unread_at=await service.latest_unread_at(current_user.id),
    )


@router.get("/unread-count", response_model=NotificationSummary)
async def get_unread_count(
    current_user: User = Depends(require_permission(PermissionEnum.NOTIFICATIONS_READ)),
    service: NotificationService = Depends(get_notification_service),
):
    """Just the counts, for a header that polls.

    Same body as ``/summary`` so a client can point either at this and get a
    working badge; ``latest_unread_at`` is simply null when nothing is unread.
    """
    return NotificationSummary(
        unread_count=await service.unread_count(current_user.id),
        high_priority_unread=await service.count_high_priority_unread(current_user.id),
    )


@router.post("/mark-all-read", response_model=NotificationSummary)
async def mark_all_read(
    current_user: User = Depends(require_permission(PermissionEnum.NOTIFICATIONS_READ)),
    service: NotificationService = Depends(get_notification_service),
):
    """Clear the whole inbox, and report what is left.

    Declared before the ``/{notification_id}`` routes so the static path can
    never be shadowed by a parameterised one that happens to be declared first.
    """
    await service.mark_all_read(current_user.id)
    return NotificationSummary(
        unread_count=await service.unread_count(current_user.id),
        high_priority_unread=0,
        latest_unread_at=None,
    )


@router.get("/{notification_id}", response_model=NotificationRead)
async def get_notification(
    notification_id: uuid.UUID,
    current_user: User = Depends(require_permission(PermissionEnum.NOTIFICATIONS_READ)),
    service: NotificationService = Depends(get_notification_service),
):
    """One notification, or not found if it is not the caller's."""
    notification = await service.get_for_user(current_user.id, notification_id)
    return NotificationRead.model_validate(notification)


@router.post("/{notification_id}/read", response_model=NotificationRead)
async def mark_notification_read(
    notification_id: uuid.UUID,
    current_user: User = Depends(require_permission(PermissionEnum.NOTIFICATIONS_READ)),
    service: NotificationService = Depends(get_notification_service),
):
    """Mark one notification read.

    Idempotent, and it keeps the original timestamp if it was already read — a
    notification being re-opened is not a second occasion of seeing it.
    """
    notification = await service.mark_read(current_user.id, notification_id)
    return NotificationRead.model_validate(notification)


@router.post("/{notification_id}/unread", response_model=NotificationRead)
async def mark_notification_unread(
    notification_id: uuid.UUID,
    current_user: User = Depends(require_permission(PermissionEnum.NOTIFICATIONS_READ)),
    service: NotificationService = Depends(get_notification_service),
):
    """Put a notification back, for the ones marked read by accident."""
    notification = await service.mark_unread(current_user.id, notification_id)
    return NotificationRead.model_validate(notification)


__all__ = ["router"]
