"""The notification event system.

Other parts of the shop need to say "this happened, tell the right person"
without knowing anything about notifications. They call :func:`emit` and move on.

The important structural decision is that **this module depends on nobody**.
``app.notifications.events`` imports no models, no services and no schemas beyond
its own. A service that wants to notify imports this one module; the notification
service imports this one module; neither imports the other. Without that,
``EstimateService`` would have to import ``NotificationService``, which imports
estimates for its own deep-link logic, and the cycle would close on the first
event anybody wired up.

The cost of that isolation is that an event is described in plain data — a
recipient, a type, some text, a loose pointer at the record — and the *rules*
about who should be told what live in the callers. That is the right way round:
"a sent estimate notifies the customer" is a fact about estimates, not a fact
about notifications, and it should be readable in the estimate service.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from app.notifications.models import PRIORITY_NORMAL

logger = logging.getLogger("autofix.notifications.events")


@dataclass(frozen=True)
class NotificationEvent:
    """One thing that happened, addressed to one user.

    A plain frozen dataclass rather than a database row or a Pydantic model,
    because an event exists only between the moment something happens and the
    moment it is written down. It is never persisted, never returned to a client
    and never re-read.

    ``recipient_id`` may be left unset when the event names a ``customer_id``
    instead: the service resolves the customer's login at delivery time. That
    indirection is the point — a customer record is what the business services
    hold, and a user id is what the notification table stores. ``deliver`` refuses
    an event carrying neither, so the gap cannot reach the database.
    """

    notification_type: str
    title: str
    recipient_id: uuid.UUID | None = None
    # Set instead of recipient_id when the sender knows the customer but not the
    # login. Resolved to a recipient at delivery, and dropped if the customer has
    # no account.
    customer_id: uuid.UUID | str | None = None
    body: str | None = None
    entity_type: str | None = None
    entity_id: uuid.UUID | str | None = None
    priority: str = PRIORITY_NORMAL
    # Optional caller-supplied idempotency key. Two identical events carrying the
    # same key produce one notification.
    dedupe_key: str | None = None

    def normalised(self) -> NotificationEvent:
        """Return a copy with the loose ids coerced to UUIDs.

        Event payloads are assembled from records whose ids are sometimes still
        strings. Coercing here means the service never has to care, and a bad id
        fails in one place instead of at the database with an opaque cast error.
        """
        return NotificationEvent(
            recipient_id=_as_uuid(self.recipient_id, self, "recipient_id"),
            customer_id=_as_uuid(self.customer_id, self, "customer_id"),
            notification_type=self.notification_type,
            title=self.title,
            body=self.body,
            entity_type=self.entity_type,
            entity_id=_as_uuid(self.entity_id, self, "entity_id"),
            priority=self.priority,
            dedupe_key=self.dedupe_key,
        )


def _as_uuid(
    value: uuid.UUID | str | None, event: NotificationEvent, field_name: str
) -> uuid.UUID | None:
    """Coerce an id to a UUID, or report the bad value and carry on without it."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        logger.warning(
            "Notification event %s had an unusable %s %r; dropping the pointer "
            "but keeping the notice",
            event.notification_type,
            field_name,
            value,
        )
        return None


@dataclass
class EventPublisher:
    """Collects events during a unit of work and writes them at the end.

    Services call :meth:`publish` as they go, and call :meth:`flush` once, at the
    same point they would have committed. Nothing is written per event, so a
    service that raises half way through leaves no notifications behind — a
    notification saying "your estimate is ready" for an estimate that was rolled
    back is worse than no notification at all.

    A bare :class:`EventPublisher` is inert: it collects and does nothing. Tests
    and services that want notifications without the side effect can pass their
    own, and callers that forget to flush simply produce no notifications rather
    than a crash.
    """

    events: list[NotificationEvent] = field(default_factory=list)

    def publish(self, event: NotificationEvent) -> None:
        """Queue one event. Cheap, synchronous, and does no I/O."""
        self.events.append(event.normalised())

    def publish_many(self, *events: NotificationEvent) -> None:
        for event in events:
            self.publish(event)

    def drain(self) -> list[NotificationEvent]:
        """Take the queued events, leaving the publisher empty.

        Draining rather than reading means a second flush in the same request
        cannot write the same event twice.
        """
        taken, self.events = self.events, []
        return taken

    def __len__(self) -> int:
        return len(self.events)


class NullPublisher(EventPublisher):
    """A publisher that throws queued events away.

    Used where notifications are genuinely not wanted — a dry-run import, a
    backfill script re-creating history that was already announced.
    """

    def publish(self, event: NotificationEvent) -> None:
        return None


__all__ = [
    "EventPublisher",
    "NotificationEvent",
    "NullPublisher",
]
