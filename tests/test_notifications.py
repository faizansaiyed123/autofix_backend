"""Tests for notifications and the event system.

The notification centre has three jobs, and the tests are grouped by which one
they are checking:

* **Delivery** — an event becomes a row, exactly once, even when the same event
  arrives twice.
* **Scoping** — a user sees their own notifications and nothing else. Every route
  derives the recipient from the token, so these tests exist to catch the day
  somebody adds a ``recipient_id`` parameter.
* **Read state** — marking read is idempotent, keeps the first timestamp, and can
  be undone.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.common.exceptions import NotFoundError
from app.customers.models import Customer
from app.estimates.models import EstimateStatus
from app.notifications.events import (
    EventPublisher,
    NotificationEvent,
    NullPublisher,
)
from app.notifications.models import (
    PRIORITY_HIGH,
    PRIORITY_NORMAL,
    Notification,
    NotificationType,
)
from app.notifications.services import NotificationService, estimate_ready_event
from tests.factories import CustomerFactory

NOTIFICATIONS = "/api/v1/notifications"
# The list route is declared at "/" like every other router in the project, so the
# collection path carries a trailing slash.
NOTIFICATION_LIST = f"{NOTIFICATIONS}/"


# --- helpers -----------------------------------------------------------------


def _event(
    recipient_id=None,
    *,
    customer_id=None,
    notification_type: str = NotificationType.ESTIMATE_READY.value,
    title: str = "Your estimate is ready",
    body: str | None = "Please review it.",
    dedupe_key: str | None = None,
    priority: str = PRIORITY_NORMAL,
    entity_id=None,
) -> NotificationEvent:
    return NotificationEvent(
        recipient_id=recipient_id,
        customer_id=customer_id,
        notification_type=notification_type,
        title=title,
        body=body,
        entity_type="estimate" if entity_id else None,
        entity_id=entity_id,
        priority=priority,
        dedupe_key=dedupe_key,
    )


async def _create_user(db: AsyncSession, label: str = "Notify") -> User:
    from app.auth.models import Role, UserRole
    from app.auth.permissions import RoleEnum
    from app.core.security import hash_password

    user = User(
        email=f"{label.lower()}-{uuid.uuid4().hex[:8]}@test.com",
        password_hash=hash_password("demo1234"),
        first_name=label,
        last_name="Tester",
        is_active=True,
        is_staff=True,
    )
    db.add(user)
    await db.flush()
    role = (
        await db.execute(select(Role).where(Role.name == RoleEnum.OWNER.value))
    ).scalar_one()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    await db.commit()
    return user


async def _client_for(db: AsyncSession, user: User):
    from httpx import ASGITransport, AsyncClient

    from app.auth.services import AuthService
    from app.core.database import get_session
    from app.main import app

    token, _ = AuthService(db).generate_tokens(user)

    async def _override():
        yield db

    app.dependency_overrides[get_session] = _override
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    )


# --- fixtures ----------------------------------------------------------------


@pytest.fixture()
async def user(db: AsyncSession):
    return await _create_user(db, "Ada")


@pytest.fixture()
async def other_user(db: AsyncSession):
    return await _create_user(db, "Grace")


@pytest.fixture()
async def user_client(db: AsyncSession, user: User):
    client = await _client_for(db, user)
    yield client
    from app.main import app

    await client.aclose()
    app.dependency_overrides.clear()


@pytest.fixture()
async def other_client(db: AsyncSession, other_user: User):
    client = await _client_for(db, other_user)
    yield client
    from app.main import app

    await client.aclose()
    app.dependency_overrides.clear()


# --- events ------------------------------------------------------------------


class TestNotificationEvents:
    def test_publisher_collects_without_writing(self):
        publisher = EventPublisher()
        publisher.publish(_event(uuid.uuid4()))

        assert len(publisher) == 1
        assert len(publisher.events) == 1

    def test_drain_empties_the_publisher(self):
        """A second flush in the same request must not write the same event twice."""
        publisher = EventPublisher()
        publisher.publish(_event(uuid.uuid4()))

        assert len(publisher.drain()) == 1
        assert len(publisher.drain()) == 0

    def test_null_publisher_discards_events(self):
        publisher = NullPublisher()
        publisher.publish(_event(uuid.uuid4()))
        assert len(publisher) == 0

    def test_string_entity_id_is_coerced(self):
        """Event payloads are built from records whose ids may still be strings."""
        raw = str(uuid.uuid4())
        event = _event(uuid.uuid4(), entity_id=raw).normalised()

        assert str(event.entity_id) == raw

    def test_unusable_entity_id_keeps_the_notice(self):
        """A bad pointer costs a deep link, not the message itself."""
        event = _event(uuid.uuid4(), entity_id="not-a-uuid").normalised()

        assert event.entity_id is None
        assert event.title == "Your estimate is ready"

    async def test_estimate_ready_event_is_keyed_on_the_estimate(self, user: User):
        """Re-sending one estimate must not stack a second notice."""
        customer_id = uuid.uuid4()
        first = estimate_ready_event(customer_id, "EST-1", "abc")
        second = estimate_ready_event(customer_id, "EST-1", "abc")
        other = estimate_ready_event(customer_id, "EST-2", "def")

        assert first.dedupe_key == second.dedupe_key
        assert first.dedupe_key != other.dedupe_key


# --- delivery ----------------------------------------------------------------


class TestNotificationDelivery:
    async def test_event_becomes_a_notification(self, db: AsyncSession, user: User):
        service = NotificationService(db)
        entity_id = uuid.uuid4()

        notification = await service.deliver(_event(user.id, entity_id=entity_id))
        await db.commit()

        assert notification is not None
        assert notification.recipient_id == user.id
        assert notification.title == "Your estimate is ready"
        assert str(notification.entity_id) == str(entity_id)
        assert notification.is_read is False

    async def test_duplicate_dedupe_key_is_suppressed(self, db: AsyncSession, user: User):
        """A retried job or a double-clicked approve produces one notice."""
        service = NotificationService(db)
        key = f"estimate_ready:{uuid.uuid4()}"

        first = await service.deliver(_event(user.id, dedupe_key=key))
        second = await service.deliver(_event(user.id, dedupe_key=key))
        await db.commit()

        assert first is not None
        assert second is None
        assert await service.count_for_user(user.id) == 1

    async def test_same_key_for_different_users_is_two_notifications(
        self, db: AsyncSession, user: User, other_user: User
    ):
        """Dedupe is per recipient, not global."""
        service = NotificationService(db)
        key = "estimate_ready:shared"

        await service.deliver(_event(user.id, dedupe_key=key))
        await service.deliver(_event(other_user.id, dedupe_key=key))
        await db.commit()

        assert await service.count_for_user(user.id) == 1
        assert await service.count_for_user(other_user.id) == 1

    async def test_events_without_a_key_never_dedupe(
        self, db: AsyncSession, user: User
    ):
        """A repeated status change is two real events, not one."""
        service = NotificationService(db)

        for _ in range(3):
            await service.deliver(_event(user.id))
        await db.commit()

        assert await service.count_for_user(user.id) == 3

    async def test_database_enforces_the_dedupe_guarantee(
        self, db: AsyncSession, user: User
    ):
        """The unique index, not the service, is what makes this true.

        A check-then-insert would let two concurrent deliveries through; the
        index is what stops it.
        """
        key = "estimate_ready:raced"
        db.add(
            Notification(
                recipient_id=user.id,
                notification_type=NotificationType.ESTIMATE_READY.value,
                title="First",
                dedupe_key=key,
            )
        )
        await db.commit()

        db.add(
            Notification(
                recipient_id=user.id,
                notification_type=NotificationType.ESTIMATE_READY.value,
                title="Second",
                dedupe_key=key,
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()

    async def test_flush_events_writes_in_the_callers_transaction(
        self, db: AsyncSession, user: User
    ):
        """A notification must not outlive the change that caused it."""
        service = NotificationService(db)
        publisher = EventPublisher()
        publisher.publish(_event(user.id))

        written = await service.flush_events(publisher)

        assert written == 1
        assert await service.unread_count(user.id) == 1

    async def test_flush_events_writes_nothing_when_empty(
        self, db: AsyncSession, user: User
    ):
        service = NotificationService(db)

        assert await service.flush_events(EventPublisher()) == 0

    async def test_deliver_all_counts_only_real_writes(
        self, db: AsyncSession, user: User
    ):
        service = NotificationService(db)
        key = f"shared:{uuid.uuid4()}"
        events = [
            _event(user.id, dedupe_key=key),
            _event(user.id, dedupe_key=key),
            _event(user.id),
        ]

        assert await service.deliver_all(events) == 2
        await db.commit()


# --- scoping -----------------------------------------------------------------


class TestNotificationScoping:
    async def test_list_is_scoped_to_the_caller(
        self, db: AsyncSession, user: User, other_user: User, user_client
    ):
        service = NotificationService(db)
        await service.deliver(_event(user.id, title="Mine"))
        await service.deliver(_event(other_user.id, title="Theirs"))
        await db.commit()

        body = (await user_client.get(NOTIFICATION_LIST)).json()

        assert [n["title"] for n in body["notifications"]] == ["Mine"]

    async def test_another_users_notification_is_not_found(
        self, db: AsyncSession, user: User, other_user: User, user_client
    ):
        service = NotificationService(db)
        theirs = await service.deliver(_event(other_user.id, title="Theirs"))
        await db.commit()

        response = await user_client.get(f"{NOTIFICATIONS}/{theirs.id}")

        # 404, not 403: a 403 would confirm the id exists.
        assert response.status_code == 404

    async def test_cannot_mark_another_users_notification_read(
        self, db: AsyncSession, user: User, other_user: User, user_client
    ):
        service = NotificationService(db)
        theirs = await service.deliver(_event(other_user.id))
        await db.commit()

        response = await user_client.post(f"{NOTIFICATIONS}/{theirs.id}/read")

        assert response.status_code == 404
        await db.refresh(theirs)
        assert theirs.read_at is None

    async def test_requires_authentication(self, unauth_client):
        assert (await unauth_client.get(NOTIFICATION_LIST)).status_code == 401


# --- listing -----------------------------------------------------------------


class TestNotificationListing:
    async def test_unread_come_before_read(
        self, db: AsyncSession, user: User, user_client
    ):
        """An attention centre is about what still needs doing."""
        service = NotificationService(db)
        old = await service.deliver(_event(user.id, title="Old unread"))
        recent = await service.deliver(_event(user.id, title="Recent unread"))
        middle = await service.deliver(_event(user.id, title="Already read"))
        await service.mark_read(user.id, middle.id)
        await db.commit()

        body = (await user_client.get(NOTIFICATION_LIST)).json()

        titles = [n["title"] for n in body["notifications"]]
        assert titles.index("Old unread") < titles.index("Recent unread")
        assert titles[-1] == "Already read"
        assert {old.id, recent.id, middle.id} == {uuid.UUID(n["id"]) for n in body["notifications"]}

    async def test_unread_only_filter(self, db: AsyncSession, user: User, user_client):
        service = NotificationService(db)
        read = await service.deliver(_event(user.id, title="Read"))
        await service.deliver(_event(user.id, title="Unread"))
        await service.mark_read(user.id, read.id)
        await db.commit()

        body = (await user_client.get(f"{NOTIFICATION_LIST}?unread_only=true")).json()

        assert [n["title"] for n in body["notifications"]] == ["Unread"]
        assert body["total"] == 2
        assert body["unread_count"] == 1

    async def test_type_filter(self, db: AsyncSession, user: User, user_client):
        service = NotificationService(db)
        await service.deliver(_event(user.id, title="Estimate"))
        await service.deliver(
            _event(
                user.id,
                title="Invoice",
                notification_type=NotificationType.INVOICE_ISSUED.value,
            )
        )
        await db.commit()

        body = (
            await user_client.get(
                f"{NOTIFICATION_LIST}?notification_type={NotificationType.INVOICE_ISSUED.value}"
            )
        ).json()

        assert [n["title"] for n in body["notifications"]] == ["Invoice"]

    async def test_limit_and_offset(self, db: AsyncSession, user: User, user_client):
        service = NotificationService(db)
        for i in range(5):
            await service.deliver(_event(user.id, title=f"Notice {i}"))
        await db.commit()

        page = (await user_client.get(f"{NOTIFICATION_LIST}?limit=2&offset=1")).json()

        assert len(page["notifications"]) == 2
        assert page["total"] == 5

    async def test_empty_inbox_is_coherent(self, user_client):
        body = (await user_client.get(NOTIFICATION_LIST)).json()

        assert body["notifications"] == []
        assert body["total"] == 0
        assert body["unread_count"] == 0


# --- read state --------------------------------------------------------------


class TestNotificationReadState:
    async def test_mark_read(self, db: AsyncSession, user: User, user_client):
        service = NotificationService(db)
        notification = await service.deliver(_event(user.id))
        await db.commit()

        body = (await user_client.post(f"{NOTIFICATIONS}/{notification.id}/read")).json()

        assert body["is_read"] is True
        assert body["read_at"] is not None

    async def test_mark_read_is_idempotent_and_keeps_the_first_time(
        self, db: AsyncSession, user: User, user_client
    ):
        """Re-opening a notification is not a second occasion of seeing it."""
        service = NotificationService(db)
        notification = await service.deliver(_event(user.id))
        await db.commit()

        first = (await user_client.post(f"{NOTIFICATIONS}/{notification.id}/read")).json()
        second = (await user_client.post(f"{NOTIFICATIONS}/{notification.id}/read")).json()

        assert first["read_at"] == second["read_at"]

    async def test_mark_unread_undoes_it(self, db: AsyncSession, user: User, user_client):
        service = NotificationService(db)
        notification = await service.deliver(_event(user.id))
        await service.mark_read(user.id, notification.id)
        await db.commit()

        body = (
            await user_client.post(f"{NOTIFICATIONS}/{notification.id}/unread")
        ).json()

        assert body["is_read"] is False
        assert body["read_at"] is None
        assert await service.unread_count(user.id) == 1

    async def test_mark_all_read(
        self, db: AsyncSession, user: User, user_client
    ):
        service = NotificationService(db)
        for _ in range(3):
            await service.deliver(_event(user.id))
        await db.commit()

        body = (await user_client.post(f"{NOTIFICATIONS}/mark-all-read")).json()

        assert body["unread_count"] == 0
        assert await service.unread_count(user.id) == 0
        assert await service.count_for_user(user.id) == 3, "marked, not deleted"

    async def test_mark_all_read_leaves_other_users_alone(
        self, db: AsyncSession, user: User, other_user: User, user_client
    ):
        service = NotificationService(db)
        await service.deliver(_event(user.id))
        await service.deliver(_event(other_user.id))
        await db.commit()

        await user_client.post(f"{NOTIFICATIONS}/mark-all-read")

        assert await service.unread_count(user.id) == 0
        assert await service.unread_count(other_user.id) == 1

    async def test_mark_all_read_on_an_empty_inbox(self, user_client):
        response = await user_client.post(f"{NOTIFICATIONS}/mark-all-read")

        assert response.status_code == 200
        assert response.json()["unread_count"] == 0


# --- badge -------------------------------------------------------------------


class TestNotificationBadge:
    async def test_unread_count(self, db: AsyncSession, user: User, user_client):
        service = NotificationService(db)
        for _ in range(2):
            await service.deliver(_event(user.id))
        await db.commit()

        body = (await user_client.get(f"{NOTIFICATIONS}/unread-count")).json()

        assert body["unread_count"] == 2

    async def test_high_priority_is_counted_separately(
        self, db: AsyncSession, user: User, user_client
    ):
        """The badge distinguishes "something needs you" from "something happened"."""
        service = NotificationService(db)
        await service.deliver(_event(user.id, priority=PRIORITY_HIGH))
        await service.deliver(_event(user.id, priority=PRIORITY_NORMAL))
        await db.commit()

        body = (await user_client.get(f"{NOTIFICATIONS}/summary")).json()

        assert body["unread_count"] == 2
        assert body["high_priority_unread"] == 1

    async def test_summary_reports_the_latest_unread(
        self, db: AsyncSession, user: User, user_client
    ):
        service = NotificationService(db)
        await service.deliver(_event(user.id))
        await db.commit()

        body = (await user_client.get(f"{NOTIFICATIONS}/summary")).json()

        assert body["latest_unread_at"] is not None

    async def test_summary_is_null_when_everything_is_read(
        self, db: AsyncSession, user: User, user_client
    ):
        service = NotificationService(db)
        notification = await service.deliver(_event(user.id))
        await service.mark_read(user.id, notification.id)
        await db.commit()

        body = (await user_client.get(f"{NOTIFICATIONS}/summary")).json()

        assert body["unread_count"] == 0
        assert body["latest_unread_at"] is None

    async def test_badge_is_not_cached_on_the_user(
        self, db: AsyncSession, user: User, user_client
    ):
        """The count is computed, so it cannot drift from the rows."""
        service = NotificationService(db)
        before = (await user_client.get(f"{NOTIFICATIONS}/unread-count")).json()

        await service.deliver(_event(user.id))
        await db.commit()
        after = (await user_client.get(f"{NOTIFICATIONS}/unread-count")).json()

        assert before["unread_count"] == 0
        assert after["unread_count"] == 1
        assert not hasattr(user, "unread_notification_count")


# --- model -------------------------------------------------------------------


class TestNotificationModel:
    def test_is_read_is_derived_from_read_at(self):
        """A stored boolean would be a second source of truth for one fact."""
        notification = Notification(
            recipient_id=uuid.uuid4(),
            notification_type=NotificationType.ESTIMATE_READY.value,
            title="x",
        )
        assert notification.is_read is False

    async def test_database_rejects_an_unknown_type(
        self, db: AsyncSession, user: User
    ):
        """The enum is enforced in the database, not only in Pydantic."""
        from sqlalchemy.exc import IntegrityError as SQLIntegrityError

        db.add(
            Notification(
                recipient_id=user.id,
                notification_type="TOTALLY_MADE_UP",
                title="x",
            )
        )
        with pytest.raises(SQLIntegrityError):
            await db.commit()
        await db.rollback()

    async def test_database_rejects_an_unknown_priority(
        self, db: AsyncSession, user: User
    ):
        from sqlalchemy.exc import IntegrityError as SQLIntegrityError

        db.add(
            Notification(
                recipient_id=user.id,
                notification_type=NotificationType.ESTIMATE_READY.value,
                title="x",
                priority="URGENT",
            )
        )
        with pytest.raises(SQLIntegrityError):
            await db.commit()
        await db.rollback()

    def test_event_default_priority_matches_the_model(self):
        """The event dataclass and the column must not drift apart."""
        column = Notification.__table__.c.priority
        default = NotificationEvent(
            notification_type="X",
            title="t",
        ).priority

        assert default == PRIORITY_NORMAL
        assert column.server_default.arg == PRIORITY_NORMAL

    async def test_unaddressed_event_is_refused(self, db: AsyncSession):
        """An event with neither a user nor a customer cannot be delivered.

        The guard that keeps a half-built event from being written against a null
        recipient, or from having its recipient guessed at.
        """
        from app.common.exceptions import BusinessRuleError

        service = NotificationService(db)
        event = NotificationEvent(
            notification_type=NotificationType.ESTIMATE_READY.value,
            title="Nobody to tell",
        )

        with pytest.raises(BusinessRuleError):
            await service.deliver(event)


# --- wiring into the shop's own services -------------------------------------


class TestNotificationWiring:
    """The events the shop actually raises.

    These are the tests that stop the notification centre from becoming a
    feature that works perfectly and is never fed.
    """

    async def _signed_in_customer(self, db: AsyncSession, label: str):
        """A customer with a login, plus a client signed in as that login.

        The client is built here rather than reusing the module's ``user_client``
        fixture, because that fixture is signed in as a *different* user — and a
        wiring test that reads somebody else's inbox passes for the wrong reason.
        """
        user = await _create_user(db, label)
        customer = CustomerFactory.build(user_id=user.id)
        db.add(customer)
        await db.commit()
        client = await _client_for(db, user)
        return customer, user, client

    async def _customer_without_login(self, db: AsyncSession, label: str) -> Customer:
        customer = CustomerFactory.build(user_id=None)
        db.add(customer)
        await db.commit()
        return customer

    @staticmethod
    def _vehicle_for(db: AsyncSession, customer: Customer):
        from tests.factories import VehicleFactory

        vehicle = VehicleFactory.build(customer_id=customer.id)
        db.add(vehicle)
        return vehicle

    async def _estimate_for(self, db: AsyncSession, customer: Customer, description: str):
        from app.estimates.schemas import EstimateCreate
        from app.estimates.services import EstimateService

        vehicle = self._vehicle_for(db, customer)
        await db.commit()
        service = EstimateService(db)
        estimate = await service.create_estimate(
            EstimateCreate(
                customer_id=customer.id,
                vehicle_id=vehicle.id,
                items=[
                    {
                        "item_type": "PART",
                        "description": description,
                        "quantity": 1,
                        "unit_price": 150.0,
                    }
                ],
            )
        )
        return service, estimate, vehicle

    async def test_sending_an_estimate_notifies_the_customer(self, db: AsyncSession):
        from app.main import app

        customer, _, client = await self._signed_in_customer(db, "EstCustomer")
        service, estimate, _ = await self._estimate_for(db, customer, "Brake pads")
        try:
            await service.send(estimate.id)

            body = (await client.get(NOTIFICATION_LIST)).json()
        finally:
            await client.aclose()
            app.dependency_overrides.clear()

        assert any(estimate.estimate_number in n["title"] for n in body["notifications"])
        assert body["notifications"][0]["notification_type"] == (
            NotificationType.ESTIMATE_READY.value
        )
        # The notice points back at the thing it is about, so the portal can
        # deep-link instead of the customer hunting for the number.
        assert body["notifications"][0]["entity_id"] == str(estimate.id)
        assert body["notifications"][0]["entity_type"] == "estimate"

    async def test_resending_an_estimate_does_not_stack_a_second_notice(
        self, db: AsyncSession
    ):
        """A customer who never opened the first one must not get two."""
        from app.main import app

        customer, _, client = await self._signed_in_customer(db, "Repeat")
        service, estimate, _ = await self._estimate_for(db, customer, "Filters")
        try:
            await service.send(estimate.id)
            await service.send(estimate.id)

            body = (await client.get(NOTIFICATION_LIST)).json()
        finally:
            await client.aclose()
            app.dependency_overrides.clear()

        assert body["total"] == 1

    async def test_a_customer_with_no_account_still_gets_their_estimate_sent(
        self, db: AsyncSession
    ):
        """No login means nobody to notify, not a failed business operation."""
        customer = await self._customer_without_login(db, "WalkIn")
        service, estimate, _ = await self._estimate_for(db, customer, "Wiper blades")

        sent = await service.send(estimate.id)

        assert sent.status == EstimateStatus.SENT.value

    async def test_a_failed_send_publishes_nothing(self, db: AsyncSession):
        """A refused send must not leave a queued event for a later save to fire."""
        customer, user, _ = await self._signed_in_customer(db, "Failed")
        service, sent_estimate, _ = await self._estimate_for(db, customer, "Battery")
        notifications = NotificationService(db)

        await service.send(sent_estimate.id)
        assert await notifications.count_for_user(user.id) == 1

        # A draft can be deleted; sending one afterwards is refused before
        # anything is published.
        _, doomed, _ = await self._estimate_for(db, customer, "Never sent")
        await service.delete_estimate(doomed.id)
        with pytest.raises(NotFoundError):
            await service.send(doomed.id)

        assert await notifications.count_for_user(user.id) == 1

    async def test_customer_without_a_user_is_never_notified(
        self, db: AsyncSession
    ):
        customer = await self._customer_without_login(db, "Silent")
        service = NotificationService(db)

        delivered = await service.notify_customer(
            customer.id, estimate_ready_event(customer.id, "EST-X", uuid.uuid4())
        )

        assert delivered is None

    async def test_issuing_an_invoice_notifies_the_customer(self, db: AsyncSession):
        """The other half of the loop: the bill reaches the customer too."""
        from app.invoices.schemas import InvoiceCreate
        from app.invoices.services import InvoiceService
        from app.main import app
        from app.repair_orders.models import RepairOrder, RepairOrderStatus

        customer, _, client = await self._signed_in_customer(db, "Billed")
        vehicle = self._vehicle_for(db, customer)
        await db.flush()
        ro = RepairOrder(
            ro_number=f"RO-{uuid.uuid4().hex[:8].upper()}",
            customer_id=customer.id,
            vehicle_id=vehicle.id,
            status=RepairOrderStatus.QC_PASSED.value,
        )
        db.add(ro)
        await db.commit()

        service = InvoiceService(db)
        invoice = await service.create_invoice(
            InvoiceCreate(
                customer_id=customer.id,
                vehicle_id=vehicle.id,
                repair_order_id=ro.id,
                extra_items=[
                    {
                        "item_type": "LABOR",
                        "description": "Brake replacement labour",
                        "quantity": 1,
                        "unit_price": 120.0,
                    }
                ],
            )
        )
        try:
            await service.issue(invoice.id)

            body = (await client.get(NOTIFICATION_LIST)).json()
        finally:
            await client.aclose()
            app.dependency_overrides.clear()

        assert any(invoice.invoice_number in n["title"] for n in body["notifications"])
        assert body["notifications"][0]["notification_type"] == (
            NotificationType.INVOICE_ISSUED.value
        )
