"""The line between the shop and a customer's own account.

A customer signs in with the same token machinery as everybody else and holds real
permissions — ``vehicles:read``, ``invoices:read``, ``payments:write`` — because
the portal needs them to see their own account. Those same permissions guard the
shop's own endpoints, and every one of those is shop-wide: ``GET
/api/v1/invoices/`` is the invoice book, ``GET /api/v1/vehicles/`` is every
vehicle on the premises, and ``GET /api/v1/payments/`` is the till.

So a permission is a statement about *what kind of work* a caller may do, and on
its own it says nothing about *whose rows* they may read. A customer holding
``invoices:read`` for their own bill could, before this boundary existed, read
every invoice in the shop — and, holding ``payments:write``, record a payment
against somebody else's.

What closes it is not a permission but a boundary: the shop's routers sit behind
``require_staff``, and a customer's own data is reached through the portal, which
derives the customer from the token and scopes every query to it.

These tests are mostly one sweep. The interesting property is not that any
particular endpoint is closed — it is that *every* shop endpoint is, including
the ones nobody thought to check, which is why the sweep walks the OpenAPI schema
rather than a hand-written list.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.auth.models import User
from app.auth.services import AuthService
from app.core.database import get_session
from app.main import app

# The three routers deliberately outside the staff gate, and why:
#   auth          — it is how a caller becomes anybody at all
#   portal        — the customer's own account, scoped by the token
#   notifications — every query filters on current_user.id
CUSTOMER_REACHABLE = ("/api/v1/auth", "/api/v1/portal", "/api/v1/notifications")

SHOP_PREFIX = "/api/v1"

# Any syntactically valid id. The staff gate runs before the endpoint looks at it,
# so the id never has to exist: what is being tested is who is asking, not what
# they are asking for.
PLACEHOLDER = str(uuid.UUID(int=1))


def _shop_read_paths() -> list[str]:
    """Every GET the shop exposes, with path parameters filled in."""
    paths = []
    for path, operations in app.openapi()["paths"].items():
        if "get" not in operations or not path.startswith(SHOP_PREFIX):
            continue
        if path.startswith(CUSTOMER_REACHABLE):
            continue
        concrete = "/".join(
            PLACEHOLDER if segment.startswith("{") else segment
            for segment in path.split("/")
        )
        paths.append(concrete)
    return sorted(paths)


class TestCustomersAreShutOutOfTheShop:
    @pytest.mark.asyncio
    async def test_every_shop_read_endpoint_refuses_a_customer(
        self, customer_client: AsyncClient
    ):
        refused = []
        for path in _shop_read_paths():
            response = await customer_client.get(path)
            assert response.status_code == 403, (
                f"{path} answered {response.status_code} to a customer; "
                "the shop's endpoints are shop-wide and must be staff-only"
            )
            refused.append(path)
        # A sweep that silently matched nothing would pass for the same reason a
        # locked door passes when the room was empty.
        assert len(refused) > 60, f"only {len(refused)} shop read endpoints were checked"

    @pytest.mark.asyncio
    async def test_the_invoice_book_is_not_a_customer_endpoint(
        self, customer_client: AsyncClient
    ):
        response = await customer_client.get("/api/v1/invoices/")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_shop_wide_takings_are_not_a_customer_endpoint(
        self, customer_client: AsyncClient
    ):
        """`payments:read` is the customer's to see their own receipts with.

        The shop's summary is every payment in the period — the till, not the
        bill — so the same permission cannot open both.
        """
        response = await customer_client.get("/api/v1/payments/summary")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_a_customer_cannot_record_a_payment_against_any_invoice(
        self, customer_client: AsyncClient
    ):
        response = await customer_client.post(
            "/api/v1/payments/",
            json={"invoice_id": PLACEHOLDER, "amount": 10.0, "method": "CASH"},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_a_customer_cannot_file_a_request_for_another_account(
        self, customer_client: AsyncClient
    ):
        response = await customer_client.post(
            "/api/v1/service_requests/",
            json={"customer_id": PLACEHOLDER, "title": "Not mine to file"},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_an_invoice_id_is_not_confirmed_to_a_customer(
        self, customer_client: AsyncClient
    ):
        """403, not 404 — and deliberately so.

        The shop's endpoints answer "you are not staff", which reveals nothing
        about whether the id exists. The portal, which does know the caller, is
        the one that answers 404, because there the id *is* the question. Two
        audiences, two honest answers.
        """
        response = await customer_client.get(f"/api/v1/invoices/{PLACEHOLDER}")
        assert response.status_code == 403


class TestStaffAreUnaffected:
    @pytest.mark.asyncio
    async def test_the_advisor_still_reads_the_invoice_book(
        self, manager_client: AsyncClient
    ):
        response = await manager_client.get("/api/v1/invoices/")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_the_owner_still_reads_the_shop(self, owner_client: AsyncClient):
        for path in ("/api/v1/invoices/", "/api/v1/vehicles/", "/api/v1/customers/"):
            response = await owner_client.get(path)
            assert response.status_code == 200, f"{path} refused the owner"

    @pytest.mark.asyncio
    async def test_a_technician_is_staff_even_where_permissions_are_thin(
        self, technician_client: AsyncClient
    ):
        """The gate is about being staff, not about holding many permissions.

        A technician reaches the shop's vehicle list — which they are allowed to
        read anyway — and is still turned away from the invoice book, which they
        are not. Two different questions, two different answers.
        """
        assert (await technician_client.get("/api/v1/vehicles/")).status_code == 200
        assert (await technician_client.get("/api/v1/invoices/")).status_code == 403


class TestTheExemptRoutesStayOpen:
    @pytest.mark.asyncio
    async def test_a_customer_keeps_their_notifications(self, customer_client: AsyncClient):
        """Notifications are the one staff-shaped router a customer may use.

        Every query in it filters on the signed-in user, so it answers "what is
        waiting for me" and never "what is waiting for the shop". Locking it would
        take away the customer's own attention centre to fix nothing.
        """
        response = await customer_client.get("/api/v1/notifications/")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_a_customer_still_reaches_the_portal_prefix(
        self, customer_client: AsyncClient
    ):
        """The portal is not gated, so a customer is not turned away at the door.

        The seeded demo login has no customer record behind it, so the portal has
        nothing to show and says 404. That is the portal's own answer about *this*
        account, and it is not the gate: 403 would be, and is what every shop
        endpoint above returns.
        """
        response = await customer_client.get("/api/v1/portal/vehicles")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_staff_may_use_the_portal_too(self, manager_client: AsyncClient):
        """The portal resolves the customer from the token, so staff get a 404.

        That is the correct answer rather than a bug: a service advisor has no
        customer record, and inventing an empty one would be a worse lie.
        """
        response = await manager_client.get("/api/v1/portal/vehicles")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_health_is_not_behind_the_gate(self, unauth_client: AsyncClient):
        assert (await unauth_client.get("/health")).status_code == 200


class TestTheBoundaryIsDecidedByRoles:
    @pytest.mark.asyncio
    async def test_a_user_created_without_the_staff_flag_is_still_staff(
        self, db, owner_client: AsyncClient
    ):
        """`is_staff` is a column a client can set, so it cannot be the boundary.

        Creating a user through the users API leaves ``is_staff`` false unless the
        caller remembers it. If the gate read that column, a technician created by
        an owner would be locked out of the shop they work in — a self-inflicted
        lockout, and one that would look like a permissions bug for an afternoon.
        """
        response = await owner_client.post(
            "/api/v1/users/",
            json={
                "email": "new-technician@autofix.demo",
                "first_name": "Nia",
                "last_name": "Newtech",
                "password": "testpass123",
                "roles": ["TECHNICIAN"],
            },
        )
        assert response.status_code == 201
        assert response.json()["is_staff"] is False

        created = (
            await db.execute(
                select(User).where(User.email == "new-technician@autofix.demo")
            )
        ).scalar_one()
        assert created.is_staff is False

        token, _ = AuthService(db).generate_tokens(created)
        async def _override():
            yield db

        app.dependency_overrides[get_session] = _override
        client = AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            # Staff by role, so the shop opens...
            assert (await client.get("/api/v1/vehicles/")).status_code == 200
            # ...and the permission still applies on top of that.
            assert (await client.get("/api/v1/invoices/")).status_code == 403
        finally:
            await client.aclose()
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_a_user_with_no_roles_at_all_is_not_staff(self, db):
        """A user the shop has de-roled keeps no access to the shop.

        With no role there is nothing to be a member of, so the gate refuses
        before any permission question is reached.
        """
        orphan = User(
            email=f"orphan-{uuid.uuid4().hex[:8]}@test.com",
            password_hash="not-a-real-hash",
            first_name="No",
            last_name="Roles",
            is_active=True,
        )
        db.add(orphan)
        await db.commit()
        token, _ = AuthService(db).generate_tokens(orphan)

        async def _override():
            yield db

        app.dependency_overrides[get_session] = _override
        client = AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            assert (await client.get("/api/v1/invoices/")).status_code == 403
        finally:
            await client.aclose()
            app.dependency_overrides.clear()
