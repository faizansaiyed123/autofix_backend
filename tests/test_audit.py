"""Tests for the audit log.

The suite is organised around the three promises this module makes, because a
failure in any one of them is a failure of the feature rather than a bug in it:

* **It records** — a sensitive route writes an entry, and the entry says who,
  what, and what the row looked like at the time.
* **It is read-only and owner-only** — no write endpoints exist, and nobody but
  the owner can read the log.
* **It survives** — the actor's email is snapshotted, so a deleted account still
  reads as somebody rather than as a null.

The decorator tests run through the real HTTP app on purpose. A unit test of the
decorator would pass whether or not it was actually attached to a route, and the
whole point of the route boundary is that somebody remembers to attach it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.decorator import default_summary
from app.audit.models import AUDIT_ACTION_VALUES, AuditAction, AuditLog
from app.audit.registry import diff_states, get_spec, load_state, registered_types
from app.audit.schemas import AuditLogRead, AuditLogSummary
from app.audit.services import AuditService
from app.auth.models import User
from app.auth.permissions import RoleEnum

AUDIT = "/api/v1/audit"
AUDIT_LIST = f"{AUDIT}/"


# --- helpers -----------------------------------------------------------------


async def _create_user(db: AsyncSession, role: RoleEnum, label: str = "Aud") -> User:
    from app.auth.models import Role, UserRole
    from app.core.security import hash_password

    user = User(
        email=f"{label.lower()}-{uuid.uuid4().hex[:8]}@test.com",
        password_hash=hash_password("demo1234"),
        first_name=label,
        last_name="Tester",
        is_active=True,
        is_staff=role is not RoleEnum.CUSTOMER,
    )
    db.add(user)
    await db.flush()
    role_row = (
        await db.execute(select(Role).where(Role.name == role.value))
    ).scalar_one()
    db.add(UserRole(user_id=user.id, role_id=role_row.id))
    await db.commit()
    return user


async def _client_for(db: AsyncSession, user: User | None = None):
    from httpx import ASGITransport, AsyncClient

    from app.auth.services import AuthService
    from app.core.database import get_session
    from app.main import app

    headers = {}
    if user is not None:
        token, _ = AuthService(db).generate_tokens(user)
        headers["Authorization"] = f"Bearer {token}"

    async def _override():
        yield db

    app.dependency_overrides[get_session] = _override
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers=headers,
    )


async def _logs_for(db: AsyncSession, **filters) -> list[AuditLog]:
    """Audit rows matching a filter, newest first — read directly, not via HTTP.

    Reading the table rather than the API is the point: these tests are checking
    what was *written*, and a bug in the read path would otherwise hide a bug in
    the write path.
    """
    conditions = []
    if "entity_type" in filters:
        conditions.append(AuditLog.entity_type == filters["entity_type"])
    if "entity_id" in filters:
        conditions.append(AuditLog.entity_id == filters["entity_id"])
    if "action" in filters:
        conditions.append(AuditLog.action == filters["action"])
    if "actor_id" in filters:
        conditions.append(AuditLog.actor_id == filters["actor_id"])
    stmt = select(AuditLog)
    if conditions:
        stmt = stmt.where(*conditions)
    result = await db.execute(stmt.order_by(AuditLog.created_at.desc()))
    return list(result.scalars().all())


# --- fixtures ----------------------------------------------------------------


@pytest.fixture()
async def owner(db: AsyncSession):
    return await _create_user(db, RoleEnum.OWNER, "Owner")


@pytest.fixture()
async def advisor(db: AsyncSession):
    return await _create_user(db, RoleEnum.SERVICE_ADVISOR, "Ada")


@pytest.fixture()
async def owner_client(db: AsyncSession, owner: User):
    client = await _client_for(db, owner)
    yield client
    from app.main import app

    await client.aclose()
    app.dependency_overrides.clear()


@pytest.fixture()
async def advisor_client(db: AsyncSession, advisor: User):
    client = await _client_for(db, advisor)
    yield client
    from app.main import app

    await client.aclose()
    app.dependency_overrides.clear()


@pytest.fixture()
async def anon_client(db: AsyncSession):
    client = await _client_for(db)
    yield client
    from app.main import app

    await client.aclose()
    app.dependency_overrides.clear()


# --- the model ---------------------------------------------------------------


class TestAuditLogModel:
    def test_action_vocabulary_is_constrained(self):
        """Every action has a value, and there are no duplicates."""
        assert len(AUDIT_ACTION_VALUES) == len(set(AUDIT_ACTION_VALUES))
        assert "CREATE" in AUDIT_ACTION_VALUES
        assert "RECORD_PAYMENT" in AUDIT_ACTION_VALUES

    async def test_unknown_action_is_rejected_by_the_database(self, db: AsyncSession):
        """The CHECK constraint does the work, not the application.

        A typo in a new call site must fail loudly. If this only held at the
        schema layer in theory, the first deploy with a widened column would
        quietly accept nonsense.
        """
        from sqlalchemy.exc import DBAPIError, IntegrityError

        db.add(
            AuditLog(
                action="NOT_A_REAL_ACTION",
                entity_type="customer",
                summary="should never be written",
            )
        )
        with pytest.raises((IntegrityError, DBAPIError)):
            await db.commit()
        await db.rollback()

    async def test_actor_label_falls_back_through_the_snapshots(self, db: AsyncSession):
        """A deleted actor still reads as somebody.

        The foreign key nulls on delete; the email is what survives. The whole
        reason both are stored.
        """
        deleted = AuditLog(action=AuditAction.DELETE.value, entity_type="customer")
        assert deleted.actor_label == "anonymous"

        with_id = AuditLog(
            action=AuditAction.DELETE.value,
            entity_type="customer",
            actor_id=uuid.uuid4(),
        )
        assert with_id.actor_label == str(with_id.actor_id)

        named = AuditLog(
            action=AuditAction.DELETE.value,
            entity_type="customer",
            actor_id=uuid.uuid4(),
            actor_email="former.advisor@shop.test",
        )
        assert named.actor_label == "former.advisor@shop.test"

    def test_repr_names_the_actor(self):
        entry = AuditLog(
            action=AuditAction.APPROVE.value,
            entity_type="estimate",
            actor_email="ada@shop.test",
        )
        assert "APPROVE" in repr(entry)
        assert "ada@shop.test" in repr(entry)


# --- the registry ------------------------------------------------------------


class TestEntityRegistry:
    def test_standard_entities_are_registered_on_import(self):
        types = registered_types()
        for expected in ("customer", "vehicle", "user", "invoice", "payment"):
            assert expected in types

    def test_unregistered_entity_is_not_an_error(self):
        """An unknown type snapshots to nothing rather than raising.

        A newly audited module should work before anybody has written its
        registry entry; the entry gets its action, its actor and its summary.
        """
        assert get_spec("hovercraft") is None

    async def test_missing_row_loads_as_none(self, db: AsyncSession):
        assert await load_state(db, "customer", uuid.uuid4()) is None
        assert await load_state(db, "hovercraft", uuid.uuid4()) is None

    async def test_password_hash_never_reaches_a_snapshot(self, db: AsyncSession):
        """A secret in a state column would outlive the account it belongs to."""
        from tests.factories import UserFactory

        user = UserFactory()
        db.add(user)
        await db.commit()

        state = await load_state(db, "user", user.id)
        assert state is not None
        assert "password_hash" not in state
        assert state["email"] == user.email


class TestDiff:
    def test_only_changed_fields_appear(self):
        before = {"status": "DRAFT", "total": 100.0, "notes": "a"}
        after = {"status": "ISSUED", "total": 100.0, "notes": "a"}
        assert diff_states(before, after) == {
            "status": {"from": "DRAFT", "to": "ISSUED"}
        }

    def test_a_field_only_one_side_describes_is_not_a_change(self):
        """Snapshots come from different places and are not the same shape.

        One is read from the table, the other from the response a route
        returned, so a key on one side alone is a difference in what was
        serialised. Treating it as a change buries the real one.
        """
        assert diff_states({"a": 1}, {"a": 1, "addresses": []}) == {}
        assert diff_states({"a": 1, "created_at": "x"}, {"a": 1}) == {}

    def test_a_shared_field_that_changed_is_reported(self):
        assert diff_states({"status": "A", "total": 1}, {"status": "B", "total": 1}) == {
            "status": {"from": "A", "to": "B"}
        }

    def test_noisy_fields_can_be_ignored(self):
        changes = diff_states(
            {"status": "A", "updated_at": "then"},
            {"status": "A", "updated_at": "now"},
            ignore=frozenset({"updated_at"}),
        )
        assert changes == {}

    def test_two_absent_states_produce_nothing(self):
        assert diff_states(None, None) == {}


# --- the service -------------------------------------------------------------


class TestAuditService:
    async def test_record_snapshots_the_actor(self, db: AsyncSession, owner: User):
        service = AuditService(db)
        entry = await service.record(
            action=AuditAction.CREATE,
            entity_type="customer",
            actor=owner,
            summary="Created a customer",
        )
        await db.commit()

        assert entry.actor_id == owner.id
        assert entry.actor_email == owner.email
        assert entry.actor_role == RoleEnum.OWNER.value

    async def test_record_flushes_into_the_callers_transaction(
        self, db: AsyncSession, owner: User
    ):
        """No commit by default, so an entry cannot outlive a rollback.

        An audit row that survives the transaction it was part of is worse than
        no audit row: it is evidence of something that never happened.
        """
        service = AuditService(db)
        actor_id = owner.id
        await service.record(
            action=AuditAction.CREATE, entity_type="customer", actor=owner
        )
        await db.rollback()
        # Read through the captured id: a rollback expires the identity map, and
        # touching `owner.id` afterwards would need a lazy load.
        assert await _logs_for(db, entity_type="customer", actor_id=actor_id) == []

    async def test_record_change_computes_the_diff(self, db: AsyncSession, owner: User):
        service = AuditService(db)
        entry = await service.record_change(
            action=AuditAction.STATUS_CHANGE,
            entity_type="estimate",
            entity_id=uuid.uuid4(),
            actor=owner,
            before_state={"status": "SENT", "total": 250.0},
            after_state={"status": "APPROVED", "total": 250.0},
        )
        await db.commit()
        assert entry.changes == {"status": {"from": "SENT", "to": "APPROVED"}}

    async def test_delete_suppresses_the_all_null_diff(
        self, db: AsyncSession, owner: User
    ):
        """A hard delete has no "after", so every field turning null is noise.

        The prior row is still captured in full; what is suppressed is the
        mechanical diff that would say all twenty columns became null.
        """
        service = AuditService(db)
        entry = await service.record_change(
            action=AuditAction.DELETE,
            entity_type="customer",
            entity_id=uuid.uuid4(),
            actor=owner,
            before_state={"email": "gone@test.com", "phone": "555"},
            after_state=None,
        )
        await db.commit()
        assert entry.changes == {}
        assert entry.before_state["email"] == "gone@test.com"
        assert entry.after_state is None

    async def test_oversized_diff_is_truncated_and_says_so(self, db: AsyncSession):
        service = AuditService(db)
        before = {f"field_{i}": i for i in range(40)}
        after = {f"field_{i}": i + 1 for i in range(40)}
        entry = await service.record_change(
            action=AuditAction.UPDATE,
            entity_type="customer",
            entity_id=uuid.uuid4(),
            before_state=before,
            after_state=after,
        )
        await db.commit()
        assert "_truncated" in entry.changes
        # The full states survive regardless; the diff is a reading aid.
        assert len(entry.before_state) == 40

    async def test_snapshots_survive_a_jsonb_round_trip(
        self, db: AsyncSession, owner: User
    ):
        """UUIDs and datetimes are stored, not silently refused.

        Every column in this schema is one of those types, so a snapshot that
        cannot be written would mean the whole feature never worked.
        """
        from tests.factories import CustomerFactory

        customer = CustomerFactory()
        db.add(customer)
        await db.commit()

        state = await load_state(db, "customer", customer.id)
        entry = await AuditService(db).record_change(
            action=AuditAction.UPDATE,
            entity_type="customer",
            entity_id=customer.id,
            actor=owner,
            after_state=state,
        )
        await db.commit()
        await db.refresh(entry)

        assert entry.after_state["id"] == str(customer.id)
        assert isinstance(entry.after_state["created_at"], str)

    async def test_label_falls_back_through_the_registry_fields(
        self, db: AsyncSession, owner: User
    ):
        from tests.factories import CustomerFactory

        customer = CustomerFactory()
        db.add(customer)
        await db.commit()

        state = await load_state(db, "customer", customer.id)
        entry = await AuditService(db).record_change(
            action=AuditAction.UPDATE,
            entity_type="customer",
            entity_id=customer.id,
            actor=owner,
            before_state=state,
            after_state=state,
        )
        await db.commit()
        assert entry.entity_label == customer.email

    async def test_listing_filters_and_paginates(self, db: AsyncSession, owner: User):
        service = AuditService(db)
        target = uuid.uuid4()
        for action in (AuditAction.CREATE, AuditAction.UPDATE, AuditAction.DELETE):
            await service.record(
                action=action,
                entity_type="customer",
                entity_id=target,
                actor=owner,
            )
        await service.record(
            action=AuditAction.CREATE, entity_type="vehicle", actor=owner
        )
        await db.commit()

        page, total = await service.list_logs(entity_id=target, size=2, page=1)
        assert total == 3
        assert len(page) == 2

        page, total = await service.list_logs(entity_id=target, size=2, page=2)
        assert total == 3
        assert len(page) == 1

        only_deletes, total = await service.list_logs(action="DELETE")
        assert total >= 1
        assert all(e.action == "DELETE" for e in only_deletes)

    async def test_unknown_filter_matches_nothing_rather_than_failing(
        self, db: AsyncSession
    ):
        """An operator typo must show an empty page, not a 500.

        The filter is free text. A log view that breaks on a mistyped action is
        a log view people stop trusting.
        """
        _, total = await AuditService(db).list_logs(action="DEFINITELY_NOT_AN_ACTION")
        assert total == 0

    async def test_entity_history_is_scoped_to_one_record(
        self, db: AsyncSession, owner: User
    ):
        service = AuditService(db)
        target = uuid.uuid4()
        other = uuid.uuid4()
        for _ in range(2):
            await service.record(
                action=AuditAction.UPDATE,
                entity_type="repair_order",
                entity_id=target,
                actor=owner,
            )
        await service.record(
            action=AuditAction.UPDATE,
            entity_type="repair_order",
            entity_id=other,
            actor=owner,
        )
        await db.commit()

        history = await service.entity_history("repair_order", target)
        assert len(history) == 2
        assert all(e.entity_id == target for e in history)


# --- the decorator, through real routes --------------------------------------


class TestAuditedRoutes:
    async def test_creating_a_customer_writes_an_entry(
        self, owner_client, db: AsyncSession, owner: User
    ):
        response = await owner_client.post(
            "/api/v1/customers/",
            json={"first_name": "Marcus", "last_name": "Reed"},
        )
        assert response.status_code == 201, response.text
        created = response.json()

        entries = await _logs_for(
            db, entity_type="customer", entity_id=created["id"], action="CREATE"
        )
        assert len(entries) == 1
        entry = entries[0]
        assert entry.actor_id == owner.id
        assert entry.actor_email == owner.email
        assert entry.actor_role == RoleEnum.OWNER.value
        assert entry.after_state["first_name"] == "Marcus"
        assert entry.before_state is None
        assert entry.ip_address is not None
        assert entry.user_agent is not None

    async def test_updating_records_the_changed_fields(
        self, owner_client, db: AsyncSession
    ):
        created = (
            await owner_client.post(
                "/api/v1/customers/",
                json={"first_name": "Nadia", "last_name": "Cole"},
            )
        ).json()

        response = await owner_client.patch(
            f"/api/v1/customers/{created['id']}", json={"phone": "555-0100"}
        )
        assert response.status_code == 200, response.text

        entries = await _logs_for(
            db, entity_type="customer", entity_id=created["id"], action="UPDATE"
        )
        assert len(entries) == 1
        changes = entries[0].changes
        assert changes["phone"] == {"from": created["phone"], "to": "555-0100"}
        # A field nobody touched must not appear, and neither must updated_at,
        # which changes on every single write.
        assert set(changes) == {"phone"}
        assert entries[0].before_state["phone"] == created["phone"]

    async def test_a_failed_request_writes_nothing(
        self, owner_client, db: AsyncSession
    ):
        """A 404 is not an event.

        The decorator runs after the handler returns, so a handler that raised
        never reaches the write. Logging the attempt to change a record that does
        not exist would fill the log with noise from mistyped ids.
        """
        await owner_client.patch(
            f"/api/v1/customers/{uuid.uuid4()}", json={"phone": "555"}
        )
        entries = await _logs_for(db, entity_type="customer", action="UPDATE")
        assert entries == []

    async def test_soft_delete_is_recorded_with_both_states(
        self, owner_client, db: AsyncSession
    ):
        """A "delete" that only flips a status is still a change to a record.

        The after state is read from the database, so the entry shows the status
        move rather than twenty columns becoming null.
        """
        created = (
            await owner_client.post(
                "/api/v1/customers/", json={"first_name": "Iris", "last_name": "Vance"}
            )
        ).json()

        response = await owner_client.delete(f"/api/v1/customers/{created['id']}")
        assert response.status_code == 204, response.text

        entries = await _logs_for(
            db, entity_type="customer", entity_id=created["id"], action="DELETE"
        )
        assert len(entries) == 1
        entry = entries[0]
        assert entry.before_state["customer_status"] == "ACTIVE"
        assert entry.after_state["customer_status"] != "ACTIVE"

    async def test_status_change_is_a_distinct_action(
        self, owner_client, db: AsyncSession
    ):
        """A lifecycle move is not an edit.

        "Changed the status of the RO" and "corrected the notes" are different
        facts, and a report asking one of those questions should not have to know
        which endpoint produced the row.
        """
        customer = (
            await owner_client.post(
                "/api/v1/customers/", json={"first_name": "Rafi", "last_name": "Cole"}
            )
        ).json()
        vehicle = (
            await owner_client.post(
                "/api/v1/vehicles/",
                json={
                    "customer_id": customer["id"],
                    "vin": "JTMW1F26XL5912345",
                    "make": "Toyota",
                    "model": "Hilux",
                },
            )
        ).json()

        response = await owner_client.patch(
            f"/api/v1/vehicles/{vehicle['id']}/status?status=IN_SHOP"
        )
        assert response.status_code == 200, response.text

        entries = await _logs_for(
            db, entity_type="vehicle", entity_id=vehicle["id"], action="STATUS_CHANGE"
        )
        assert len(entries) == 1
        assert entries[0].changes["status"] == {"from": "ACTIVE", "to": "IN_SHOP"}
        assert entries[0].entity_label == vehicle["vin"]

    async def test_nested_routes_audit_the_parent_not_the_child(
        self, owner_client, db: AsyncSession
    ):
        """`/estimates/{id}/items/{item_id}/decision` is an event about the estimate.

        A path with two ids must not file the entry under whichever id happened
        to be declared last, and the item id is not a registered entity anyway.
        """
        customer = (
            await owner_client.post(
                "/api/v1/customers/", json={"first_name": "Sol", "last_name": "Bello"}
            )
        ).json()
        vehicle = (
            await owner_client.post(
                "/api/v1/vehicles/",
                json={
                    "customer_id": customer["id"],
                    "vin": "2HGFC2F59JH512345",
                    "make": "Honda",
                    "model": "Civic",
                },
            )
        ).json()
        created_estimate = await owner_client.post(
            "/api/v1/estimates/",
            json={
                "customer_id": customer["id"],
                "vehicle_id": vehicle["id"],
                "items": [
                    {
                        "item_type": "PART",
                        "description": "Brake pads",
                        "quantity": 1,
                        "unit_price": 120.0,
                    }
                ],
            },
        )
        assert created_estimate.status_code == 201, created_estimate.text
        estimate = created_estimate.json()
        item_id = estimate["items"][0]["id"]
        sent = await owner_client.post(f"/api/v1/estimates/{estimate['id']}/send")
        assert sent.status_code == 200, sent.text

        response = await owner_client.post(
            f"/api/v1/estimates/{estimate['id']}/items/{item_id}/decision",
            json={"decision": "APPROVED"},
        )
        assert response.status_code == 200, response.text

        entries = [
            e
            for e in await _logs_for(db, entity_type="estimate", action="DECIDE")
            if str(e.entity_id) == estimate["id"]
        ]
        assert len(entries) == 1
        assert str(entries[0].entity_id) == estimate["id"]
        assert entries[0].entity_label == estimate["estimate_number"]

    async def test_the_proxy_header_wins_over_the_socket_address(
        self, owner_client, db: AsyncSession
    ):
        """Behind a reverse proxy, the socket address is the proxy's.

        Only the leftmost X-Forwarded-For entry is taken: that is the client as
        the first hop saw it, and the rest of the chain is the shop's own
        infrastructure.
        """
        created = (
            await owner_client.post(
                "/api/v1/customers/",
                json={"first_name": "Bea", "last_name": "Ng"},
                headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
            )
        ).json()

        entry = (
            await _logs_for(
                db, entity_type="customer", entity_id=created["id"], action="CREATE"
            )
        )[0]
        assert entry.ip_address == "203.0.113.7"

    async def test_summaries_read_as_english(self):
        assert default_summary(AuditAction.CREATE, "customer") == "Created a customer"
        assert (
            default_summary(AuditAction.ISSUE, "invoice") == "Issued an invoice"
        )
        assert (
            default_summary(AuditAction.STATUS_CHANGE, "repair_order")
            == "Changed status of a repair order"
        )


# --- authentication ----------------------------------------------------------


class TestAuthEvents:
    async def test_a_successful_sign_in_is_recorded(self, db: AsyncSession, owner: User):
        client = await _client_for(db)
        try:
            response = await client.post(
                "/api/v1/auth/login",
                json={"email": owner.email, "password": "demo1234"},
            )
            assert response.status_code == 200, response.text
        finally:
            from app.main import app

            await client.aclose()
            app.dependency_overrides.clear()

        entries = await _logs_for(db, entity_type="user", action="LOGIN")
        assert len(entries) == 1
        assert entries[0].actor_id == owner.id
        assert entries[0].entity_label == owner.email

    async def test_a_failed_sign_in_is_recorded_without_an_actor(
        self, db: AsyncSession
    ):
        """The interesting failure is the one nobody sees from the inside.

        There is no user to attach — that is the point — so the entry carries the
        attempted address and no foreign key, and a run of them is visible in the
        log as the pattern it is.
        """
        client = await _client_for(db)
        try:
            response = await client.post(
                "/api/v1/auth/login",
                json={"email": "intruder@nowhere.test", "password": "wrong"},
            )
            assert response.status_code == 401
        finally:
            from app.main import app

            await client.aclose()
            app.dependency_overrides.clear()

        entries = await _logs_for(db, entity_type="user", action="LOGIN_FAILED")
        assert len(entries) == 1
        entry = entries[0]
        assert entry.actor_id is None
        assert entry.actor_label == "anonymous"
        assert entry.entity_label == "intruder@nowhere.test"

    async def test_a_wrong_password_does_not_succeed(self, db: AsyncSession, owner: User):
        client = await _client_for(db)
        try:
            response = await client.post(
                "/api/v1/auth/login",
                json={"email": owner.email, "password": "not-the-password"},
            )
            assert response.status_code == 401
        finally:
            from app.main import app

            await client.aclose()
            app.dependency_overrides.clear()

        entries = await _logs_for(db, entity_type="user", action="LOGIN_FAILED")
        assert entries[-1].entity_label == owner.email
        # The failed attempt must never be recorded as a success.
        assert not [e for e in await _logs_for(db, action="LOGIN") if e.actor_id == owner.id]


# --- the API -----------------------------------------------------------------


class TestAuditApi:
    async def test_the_log_is_owner_only(self, advisor_client, anon_client):
        assert (await advisor_client.get(AUDIT_LIST)).status_code == 403
        assert (await anon_client.get(AUDIT_LIST)).status_code == 401

    async def test_the_owner_can_read_the_log(self, owner_client, db: AsyncSession):
        await AuditService(db).record(
            action=AuditAction.APPROVE,
            entity_type="estimate",
            entity_id=uuid.uuid4(),
            summary="Approved an estimate",
        )
        await db.commit()

        response = await owner_client.get(AUDIT_LIST)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["meta"]["total"] >= 1
        assert body["data"][0]["action"] == "APPROVE"

    async def test_listing_omits_the_heavy_state_columns(
        self, owner_client, db: AsyncSession
    ):
        """Two hundred entries with two full snapshots each is nobody's idea of a list.

        The detail endpoint has them; the list has the diff, which is what a list
        actually renders.
        """
        target = uuid.uuid4()
        service = AuditService(db)
        await service.record_change(
            action=AuditAction.UPDATE,
            entity_type="customer",
            entity_id=target,
            before_state={"phone": "old"},
            after_state={"phone": "new"},
        )
        await db.commit()

        entry = (await owner_client.get(AUDIT_LIST)).json()["data"][0]
        assert "before_state" not in entry
        assert "after_state" not in entry
        assert entry["changes"] == {"phone": {"from": "old", "to": "new"}}

    async def test_the_detail_endpoint_has_the_states(self, owner_client, db: AsyncSession):
        target = uuid.uuid4()
        entry = await AuditService(db).record_change(
            action=AuditAction.UPDATE,
            entity_type="customer",
            entity_id=target,
            before_state={"phone": "old"},
            after_state={"phone": "new"},
        )
        await db.commit()

        response = await owner_client.get(f"{AUDIT}/{entry.id}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["before_state"] == {"phone": "old"}
        assert body["after_state"] == {"phone": "new"}

    async def test_a_missing_entry_is_a_404(self, owner_client):
        assert (await owner_client.get(f"{AUDIT}/{uuid.uuid4()}")).status_code == 404

    async def test_the_date_filter_is_inclusive_of_the_last_day(
        self, owner_client, db: AsyncSession
    ):
        """A range ending "today" has to include what happened today.

        Comparing against midnight of the end date would drop every entry written
        since, which is most of the ones anybody is looking for.
        """
        from datetime import time

        service = AuditService(db)
        today = datetime.now(UTC).date()
        late_today = await service.record(action=AuditAction.CREATE, entity_type="customer")
        yesterday = await service.record(action=AuditAction.CREATE, entity_type="customer")
        late_today.created_at = datetime.combine(today, time(23, 59), tzinfo=UTC)
        yesterday.created_at = datetime.combine(
            today - timedelta(days=1), time(12, 0), tzinfo=UTC
        )
        db.add_all([late_today, yesterday])
        await db.commit()

        window = f"date_from={today.isoformat()}&date_to={today.isoformat()}"
        body = (await owner_client.get(f"{AUDIT_LIST}?{window}")).json()
        ids = {e["id"] for e in body["data"]}
        assert str(late_today.id) in ids
        assert str(yesterday.id) not in ids

    async def test_entity_history_is_scoped_to_one_record(self, owner_client, db: AsyncSession):
        target, other = uuid.uuid4(), uuid.uuid4()
        service = AuditService(db)
        for _ in range(2):
            await service.record(
                action=AuditAction.UPDATE, entity_type="invoice", entity_id=target
            )
        await service.record(
            action=AuditAction.UPDATE, entity_type="invoice", entity_id=other
        )
        await db.commit()

        response = await owner_client.get(f"{AUDIT}/entity/invoice/{target}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 2
        assert all(e["entity_id"] == str(target) for e in body["entries"])

    async def test_the_vocabulary_endpoints_match_the_constraint(
        self, owner_client
    ):
        """A client that renders these cannot offer a value the database rejects."""
        actions = (await owner_client.get(f"{AUDIT}/actions")).json()["actions"]
        assert actions == list(AUDIT_ACTION_VALUES)

        types = (await owner_client.get(f"{AUDIT}/entity-types")).json()["entity_types"]
        assert "customer" in types

    async def test_there_is_no_way_to_write_through_the_api(self, owner_client):
        """Read-only is the feature, so it is asserted rather than assumed.

        An endpoint that can erase audit rows is an endpoint for erasing evidence.
        """
        from app.main import app

        audit_paths = [p for p in app.openapi()["paths"] if "/audit" in p]
        assert audit_paths, "the audit router should be mounted"
        for path, operations in app.openapi()["paths"].items():
            if "/audit" not in path:
                continue
            assert set(operations) <= {"get"}, f"{path} exposes {set(operations)}"

        for method in ("post", "put", "patch", "delete"):
            assert (await owner_client.request(method.upper(), AUDIT_LIST)).status_code == 405


# --- schemas -----------------------------------------------------------------


class TestSchemas:
    def test_summary_omits_states_but_keeps_the_actor(self, owner: User):
        entry = _entry(owner)
        summary = AuditLogSummary.from_row(entry)
        assert summary.actor_label == owner.email
        assert not hasattr(summary, "before_state")
        assert summary.changes == {"a": {"from": 1, "to": 2}}

    def test_read_keeps_everything(self, owner: User):
        entry = _entry(owner)
        read = AuditLogRead.from_row(entry)
        assert read.actor_id == owner.id
        assert read.actor_label == owner.email
        assert read.before_state == {"a": 1}
        assert read.after_state == {"a": 2}


def _entry(owner: User) -> AuditLog:
    """A persisted-shaped entry, without touching the database.

    ``id`` and ``created_at`` are server defaults, so an instance built by hand
    carries ``None`` for both — which is a fair thing for a model to do and not a
    thing a response schema should have to tolerate.
    """
    return AuditLog(
        id=uuid.uuid4(),
        created_at=datetime.now(UTC),
        action=AuditAction.UPDATE.value,
        entity_type="customer",
        actor_id=owner.id,
        actor_email=owner.email,
        actor_role=RoleEnum.OWNER.value,
        before_state={"a": 1},
        after_state={"a": 2},
        changes={"a": {"from": 1, "to": 2}},
    )
