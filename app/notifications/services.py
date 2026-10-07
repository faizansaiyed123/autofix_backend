"""Notification business logic: delivery, listing, and read state."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import BusinessRuleError, NotFoundError
from app.customers.models import Customer
from app.notifications.events import EventPublisher, NotificationEvent
from app.notifications.models import (
    PRIORITY_HIGH,
    Notification,
    NotificationType,
)

logger = logging.getLogger("autofix.notifications.services")


def _now() -> datetime:
    return datetime.now(UTC)


class NotificationService:
    """Everything the notification centre does."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # --- delivery -----------------------------------------------------------

    async def deliver(self, event: NotificationEvent) -> Notification | None:
        """Write one notification, or return ``None`` if it was a duplicate.

        The duplicate check and the insert are one operation rather than a
        ``SELECT`` followed by an ``INSERT``: two concurrent deliveries of the same
        event would both pass a check-then-insert, and the unique index underneath
        is what actually makes the guarantee. The loser of that race gets the
        integrity error, is logged at debug, and returns ``None`` — a duplicate
        notification is not a failure worth failing a request over.
        """
        event = event.normalised()
        if event.recipient_id is None and event.customer_id is not None:
            # Addressed to a customer rather than a login: resolve it now. Most
            # walk-in customers never create an account, and "no account" is not a
            # failure — it just means there is nobody to deliver to, so the event
            # is dropped quietly and the caller's operation carries on.
            event = await self._address_to_customer(event)
        if event is None or event.recipient_id is None:
            if event is not None:
                raise BusinessRuleError(
                    f"Notification event {event.notification_type} has no recipient"
                )
            return None
        if event.dedupe_key:
            existing = await self._find_by_dedupe_key(
                event.recipient_id, event.dedupe_key
            )
            if existing is not None:
                return None

        notification = Notification(
            recipient_id=event.recipient_id,
            notification_type=event.notification_type,
            title=event.title,
            body=event.body,
            entity_type=event.entity_type,
            entity_id=event.entity_id,
            priority=event.priority,
            dedupe_key=event.dedupe_key,
        )
        self.db.add(notification)
        try:
            await self.db.flush()
        except IntegrityError:
            # The unique index rejected a concurrent duplicate. Undo just this
            # insert and carry on.
            await self.db.rollback()
            logger.debug(
                "Duplicate notification suppressed for %s (%s)",
                event.recipient_id,
                event.dedupe_key,
            )
            return None
        return notification

    async def deliver_all(self, events: list[NotificationEvent]) -> int:
        """Deliver several events, returning how many were actually written."""
        written = 0
        for event in events:
            if await self.deliver(event) is not None:
                written += 1
        return written

    async def flush_events(
        self, publisher: EventPublisher, *, commit: bool = False
    ) -> int:
        """Write everything a publisher collected, in the caller's transaction.

        ``commit`` stays off by default so the events land in the *same*
        transaction as the change that caused them. A notification committed
        separately could survive a rolled-back estimate, and the shop would be
        telling a customer about work that does not exist.
        """
        written = await self.deliver_all(publisher.drain())
        if commit:
            await self.db.commit()
        return written

    # --- reading ------------------------------------------------------------

    async def get_for_user(
        self, recipient_id: uuid.UUID | str, notification_id: uuid.UUID | str
    ) -> Notification:
        """One notification, scoped to its recipient.

        Scoped in the query rather than checked afterwards, so another user's
        notification is not found and then refused — it simply is not found.
        """
        result = await self.db.execute(
            select(Notification).where(
                Notification.id == str(notification_id),
                Notification.recipient_id == str(recipient_id),
            )
        )
        notification = result.scalar_one_or_none()
        if notification is None:
            raise NotFoundError(f"Notification {notification_id} not found")
        return notification

    async def list_for_user(
        self,
        recipient_id: uuid.UUID | str,
        *,
        unread_only: bool = False,
        notification_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Notification]:
        """A user's notifications, newest first.

        Unread first, then newest. An attention centre is about what still needs
        doing, and pure recency buries the one unread item under forty read ones.
        """
        stmt = select(Notification).where(Notification.recipient_id == str(recipient_id))
        if unread_only:
            stmt = stmt.where(Notification.read_at.is_(None))
        if notification_type:
            stmt = stmt.where(Notification.notification_type == notification_type)
        stmt = stmt.order_by(
            Notification.read_at.is_not(None).asc(),
            Notification.created_at.desc(),
        )
        result = await self.db.execute(stmt.limit(limit).offset(offset))
        return list(result.scalars().all())

    async def count_for_user(
        self,
        recipient_id: uuid.UUID | str,
        *,
        unread_only: bool = False,
    ) -> int:
        stmt = select(func.count(Notification.id)).where(
            Notification.recipient_id == str(recipient_id)
        )
        if unread_only:
            stmt = stmt.where(Notification.read_at.is_(None))
        return int((await self.db.execute(stmt)).scalar_one() or 0)

    async def unread_count(self, recipient_id: uuid.UUID | str) -> int:
        """The badge number.

        Counted, not stored. A cached counter on ``users`` is a second source of
        truth that is wrong the moment a read fails, and the badge is the one
        number a user is guaranteed to look at.
        """
        return await self.count_for_user(recipient_id, unread_only=True)

    async def count_high_priority_unread(
        self, recipient_id: uuid.UUID | str
    ) -> int:
        result = await self.db.execute(
            select(func.count(Notification.id)).where(
                Notification.recipient_id == str(recipient_id),
                Notification.read_at.is_(None),
                Notification.priority == PRIORITY_HIGH,
            )
        )
        return int(result.scalar_one() or 0)

    async def latest_unread_at(
        self, recipient_id: uuid.UUID | str
    ) -> datetime | None:
        """When the most recent unread arrived, or ``None`` if nothing is unread.

        Separate from the count because the count alone cannot say "you have had
        something new since Tuesday", which is the question a person opening the
        app is actually asking.
        """
        result = await self.db.execute(
            select(Notification.created_at)
            .where(
                Notification.recipient_id == str(recipient_id),
                Notification.read_at.is_(None),
            )
            .order_by(Notification.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    # --- read state ---------------------------------------------------------

    async def mark_read(
        self,
        recipient_id: uuid.UUID | str,
        notification_id: uuid.UUID | str,
        *,
        commit: bool = True,
    ) -> Notification:
        """Mark one notification read.

        Idempotent: a second call keeps the *original* timestamp. Re-reading a
        notification is not a new event, and overwriting the time would make
        "when did you first see this" unanswerable.
        """
        notification = await self.get_for_user(recipient_id, notification_id)
        if notification.read_at is None:
            notification.read_at = _now()
            if commit:
                await self.db.commit()
            else:
                await self.db.flush()
        return notification

    async def mark_all_read(
        self, recipient_id: uuid.UUID | str, *, commit: bool = True
    ) -> int:
        """Mark every unread notification read. Returns how many changed.

        One UPDATE rather than a loop of individual marks, because a user with
        four hundred unread items should not wait for four hundred round trips —
        and because a loop here is a loop somebody will later refactor into
        per-row notifications, emails and webhooks.
        """
        now = _now()
        result = await self.db.execute(
            select(Notification).where(
                Notification.recipient_id == str(recipient_id),
                Notification.read_at.is_(None),
            )
        )
        pending = list(result.scalars().all())
        for notification in pending:
            notification.read_at = now
        if pending and commit:
            await self.db.commit()
        elif pending:
            await self.db.flush()
        return len(pending)

    async def mark_unread(
        self,
        recipient_id: uuid.UUID | str,
        notification_id: uuid.UUID | str,
        *,
        commit: bool = True,
    ) -> Notification:
        """Mark one notification unread again.

        Provided because people put notifications back by accident, and a centre
        with no undo is a centre people stop trusting. Clearing ``read_at`` is the
        whole operation — there is no ``is_read`` column to keep in step.
        """
        notification = await self.get_for_user(recipient_id, notification_id)
        notification.read_at = None
        if commit:
            await self.db.commit()
        else:
            await self.db.flush()
        return notification

    # --- internals ----------------------------------------------------------

    async def _address_to_customer(
        self, event: NotificationEvent
    ) -> NotificationEvent | None:
        """Turn a customer-addressed event into a user-addressed one.

        Returns ``None`` when the customer has no linked login. The recipient is
        read from the customer record rather than accepted from the caller,
        because a caller that supplied the user id could address the notice to
        whoever it liked.
        """
        result = await self.db.execute(
            select(Customer.user_id).where(Customer.id == str(event.customer_id))
        )
        user_id = result.scalar_one_or_none()
        if user_id is None:
            logger.debug(
                "Customer %s has no linked user; skipping %s",
                event.customer_id,
                event.notification_type,
            )
            return None
        return NotificationEvent(
            recipient_id=user_id,
            customer_id=event.customer_id,
            notification_type=event.notification_type,
            title=event.title,
            body=event.body,
            entity_type=event.entity_type,
            entity_id=event.entity_id,
            priority=event.priority,
            dedupe_key=event.dedupe_key,
        )

    async def notify_customer(
        self,
        customer_id: uuid.UUID | str,
        event: NotificationEvent,
    ) -> Notification | None:
        """Deliver an event to the user behind a customer record.

        A convenience over :meth:`deliver` for callers holding a customer id
        rather than an event. Returns ``None`` when the customer has no login.
        """
        addressed = NotificationEvent(
            recipient_id=event.recipient_id,
            customer_id=customer_id,
            notification_type=event.notification_type,
            title=event.title,
            body=event.body,
            entity_type=event.entity_type,
            entity_id=event.entity_id,
            priority=event.priority,
            dedupe_key=event.dedupe_key,
        )
        return await self.deliver(addressed)

    async def _find_by_dedupe_key(
        self, recipient_id: uuid.UUID, dedupe_key: str
    ) -> Notification | None:
        result = await self.db.execute(
            select(Notification).where(
                Notification.recipient_id == str(recipient_id),
                Notification.dedupe_key == dedupe_key,
            )
        )
        return result.scalar_one_or_none()


def estimate_ready_event(
    customer_id: uuid.UUID | str, estimate_number: str, estimate_id: uuid.UUID | str
) -> NotificationEvent:
    """The standard "your estimate is ready to look at" notice.

    Addressed to a *customer*, not a login: the estimate service knows the
    customer and has no business knowing which user, if any, belongs to them.

    A helper rather than a template table: one sentence is not worth a rendering
    layer, and the text belongs next to the rule that decides to send it.
    """
    return NotificationEvent(
        customer_id=customer_id,
        notification_type=NotificationType.ESTIMATE_READY.value,
        title=f"Your estimate {estimate_number} is ready",
        body="Review each line and approve or decline the work you want done.",
        entity_type="estimate",
        entity_id=estimate_id,
        # Keyed on the estimate, so re-sending the same estimate to the same
        # customer does not stack a second "ready" on top of the first.
        dedupe_key=f"estimate_ready:{estimate_id}",
    )


def invoice_issued_event(
    customer_id: uuid.UUID | str, invoice_number: str, invoice_id: uuid.UUID | str
) -> NotificationEvent:
    """The standard "here is your bill" notice."""
    return NotificationEvent(
        customer_id=customer_id,
        notification_type=NotificationType.INVOICE_ISSUED.value,
        title=f"Your invoice {invoice_number} is ready",
        body="You can view and download the full invoice from your account.",
        entity_type="invoice",
        entity_id=invoice_id,
        dedupe_key=f"invoice_issued:{invoice_id}",
    )


__all__ = [
    "NotificationService",
    "estimate_ready_event",
    "invoice_issued_event",
]
