"""In-app notification model.

A notification is a **record that something happened to somebody**, addressed to
one user. That framing decides most of the design:

* The recipient is a :class:`~app.auth.models.User`, not a customer. Staff need
  these too — "a part request is waiting on your approval" is the notification the
  shop's whole attention centre is built on. Addressing everything at customers
  would make the table useless to the people who work here.
* There is **no foreign key on the thing being notified about**. ``entity_type``
  and ``entity_id`` are a loose pointer, because the events span estimates,
  invoices, repair orders, appointments and purchase orders. A real FK would mean
  five nullable columns and a cascade that deletes a customer's history because
  somebody archived a vehicle.
* There is **no update and no delete**. Marking as read is the only mutation. A
  notification is a fact about a moment; editing the text afterwards would make
  the record disagree with what was actually sent, and deleting it removes the
  only trace that the shop told anybody anything.
* ``is_read`` is **derived from ``read_at``**, not stored beside it. Two columns
  for one fact is two columns to drift, and the drift is always in the direction
  of a notification that looks read and never was.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import TimestampedBase


class NotificationType(str, enum.Enum):
    """What kind of event produced a notification.

    The value is stored, not the enum, so a row stays readable in ``psql`` years
    from now. Templates and per-type preferences are deliberately *not* here: a
    notification is one sentence of plain text, and a rendering layer is a
    separate decision from storing the fact.
    """

    ESTIMATE_READY = "ESTIMATE_READY"
    ESTIMATE_DECIDED = "ESTIMATE_DECIDED"
    INVOICE_ISSUED = "INVOICE_ISSUED"
    PAYMENT_RECEIVED = "PAYMENT_RECEIVED"
    INVOICE_OVERDUE = "INVOICE_OVERDUE"
    REPAIR_ORDER_UPDATE = "REPAIR_ORDER_UPDATE"
    READY_FOR_PICKUP = "READY_FOR_PICKUP"
    APPOINTMENT_UPDATE = "APPOINTMENT_UPDATE"
    PART_REQUEST_SUBMITTED = "PART_REQUEST_SUBMITTED"
    PART_REQUEST_APPROVED = "PART_REQUEST_APPROVED"
    PURCHASE_ORDER_UPDATE = "PURCHASE_ORDER_UPDATE"
    INVENTORY_LOW = "INVENTORY_LOW"


NOTIFICATION_TYPE_VALUES: tuple[str, ...] = tuple(t.value for t in NotificationType)

# How urgent a notification is, which is what the attention centre sorts on.
PRIORITY_NORMAL = "NORMAL"
PRIORITY_HIGH = "HIGH"
PRIORITY_VALUES: tuple[str, ...] = (PRIORITY_NORMAL, PRIORITY_HIGH)


class Notification(TimestampedBase):
    """One unread-or-read notice for one user."""

    __tablename__ = "notifications"

    recipient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    notification_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)

    # A loose pointer to whatever the notification is about, so the client can
    # deep-link. Deliberately not a foreign key — see the module docstring.
    entity_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    priority: Mapped[str] = mapped_column(
        String(10), nullable=False, default=PRIORITY_NORMAL, server_default=PRIORITY_NORMAL
    )

    # Set once, when the user acts on it. Null means unread.
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Suppresses a repeat of an event that has already been delivered. A retried
    # job, a double-clicked approve, or the same estimate being sent to the same
    # advisor twice should produce one notice, not two — two identical unread
    # rows is how a notification centre loses the reader's trust.
    dedupe_key: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "notification_type IN " + str(NOTIFICATION_TYPE_VALUES),
            name="ck_notifications_type",
        ),
        CheckConstraint(
            "priority IN " + str(PRIORITY_VALUES), name="ck_notifications_priority"
        ),
        # The attention centre asks for "my unread, newest first" and the badge
        # asks for the count. One index serves both.
        Index("ix_notifications_recipient_unread", "recipient_id", "read_at"),
        # The guarantee behind deduplication, enforced by the database rather than
        # by a check-then-insert in the service — which two concurrent deliveries
        # of the same event would both pass. Partial, because most notifications
        # have no key and should not each occupy a row in the index.
        Index(
            "uq_notifications_dedupe",
            "recipient_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL"),
        ),
    )

    @property
    def is_read(self) -> bool:
        """Whether the user has seen this.

        A stored boolean would be a second source of truth for a fact that
        ``read_at`` already records exactly — and the two would drift, always in
        the direction of a notification that looks read and never was.
        """
        return self.read_at is not None

    def __repr__(self) -> str:
        return (
            f"<Notification {self.notification_type} "
            f"to={self.recipient_id} read={self.is_read}>"
        )


__all__ = [
    "NOTIFICATION_TYPE_VALUES",
    "PRIORITY_HIGH",
    "PRIORITY_NORMAL",
    "PRIORITY_VALUES",
    "Notification",
    "NotificationType",
]
