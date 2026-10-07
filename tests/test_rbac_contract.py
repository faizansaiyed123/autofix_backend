"""Contract tests for RBAC and the claims the tracker makes about it.

Three separate promises are easy to break quietly, and none of them fails loudly
when it does:

* **A permission a route asks for exists.** ``require_permission`` takes a
  ``PermissionEnum`` member, so a typo is an ``AttributeError`` at import — but a
  *hand-written* ``Permission(name="invoices:reed")`` row, or a route reached
  through a string, would sail through and produce an endpoint no role can open.
* **The grants match the catalogue.** ``ROLE_PERMISSIONS`` is the intent;
  ``role_permissions`` is what the seeder wrote. They are built from the same
  object graph today, so drift is impossible without somebody editing one side.
* **The tracker's numbers are true.** "13 endpoints" is a claim about the code
  that nothing checks, so it stays correct only while nobody edits a router.

These tests are cheap and mostly structural. They exist so that the *next* person
to add a permission, a role grant or an endpoint finds out here rather than in
production.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.appointments.routes import router as appointments_router
from app.audit.routes import router as audit_router
from app.auth.models import Permission, Role, RolePermission
from app.auth.permissions import ROLE_PERMISSIONS, PermissionEnum, RoleEnum
from app.customers.routes import router as customers_router
from app.invoices.routes import router as invoices_router
from app.main import app
from app.notifications.routes import router as notifications_router
from app.payments.routes import router as payments_router
from app.portal.routes import router as portal_router
from app.reports.routes import router as reports_router
from app.vehicles.routes import router as vehicles_router

# Every router the application mounts, with the prefix main.py gives it. The
# prefix is repeated here on purpose: it is the join between a module and the
# URL the client uses, and a router that moves is a router whose guard moved too.
ROUTERS: list[tuple[object, str]] = [
    (appointments_router, "/api/v1/appointments"),
    (audit_router, "/api/v1/audit"),
    (customers_router, "/api/v1/customers"),
    (invoices_router, "/api/v1/invoices"),
    (notifications_router, "/api/v1/notifications"),
    (payments_router, "/api/v1/payments"),
    (portal_router, "/api/v1/portal"),
    (reports_router, "/api/v1/reports"),
    (vehicles_router, "/api/v1/vehicles"),
]

# The customer portal and the personal notification centre are the two places a
# non-staff account is meant to be, so they are the two that may hold permissions
# a customer also has. Everywhere else, a permission held by a customer is a
# mistake waiting to be read as a leak.
CUSTOMER_REACHABLE = ("/api/v1/portal", "/api/v1/notifications")

TRACKER = Path(__file__).resolve().parents[2] / "PROJECT_PROGRESS.md"


def _permissions_in_closure(func: Callable, depth: int = 0) -> set[PermissionEnum]:
    """Find the permissions a guard was built with.

    ``require_permission(X)`` returns a closure over ``X``, so the permission is
    not in the signature — it is in the closure cells. Walking them is how the
    route's requirement is recovered without reading source.
    """
    found: set[PermissionEnum] = set()
    if depth > 4:
        return found
    for cell in getattr(func, "__closure__", None) or ():
        try:
            contents = cell.cell_contents
        except ValueError:  # an empty cell, e.g. a not-yet-bound recursive name
            continue
        if isinstance(contents, PermissionEnum):
            found.add(contents)
        elif isinstance(contents, (tuple, list, set, frozenset)):
            found |= {item for item in contents if isinstance(item, PermissionEnum)}
        elif callable(contents):
            found |= _permissions_in_closure(contents, depth + 1)
    return found


def _route_permissions(route) -> set[PermissionEnum]:
    """Every permission the route's whole dependency chain asks for."""
    found: set[PermissionEnum] = set()
    stack = [route.dependant]
    seen: set[int] = set()
    while stack:
        dependant = stack.pop()
        if id(dependant) in seen:
            continue
        seen.add(id(dependant))
        call = getattr(dependant, "call", None)
        if call is not None:
            found |= _permissions_in_closure(call)
        stack.extend(getattr(dependant, "dependencies", []))
    return found


def _all_routes() -> Iterable[tuple[str, str, object]]:
    """(prefix, path, route) for every route in every mounted router."""
    for router, prefix in ROUTERS:
        for route in router.routes:
            yield prefix, route.path, route


class TestThePermissionCatalogue:
    @pytest.mark.asyncio
    async def test_the_seeded_permissions_are_exactly_the_catalogue(
        self, db: AsyncSession
    ):
        """No more rows than the catalogue, and no fewer.

        Equality in both directions: an extra row is a permission somebody invented
        outside the catalogue and no role can hold, and a missing one is an
        endpoint that has quietly become unreachable for everybody.
        """
        seeded = set((await db.execute(select(Permission.name))).scalars())
        assert seeded == {p.value for p in PermissionEnum}

    @pytest.mark.asyncio
    async def test_every_role_is_granted_exactly_what_the_catalogue_says(
        self, db: AsyncSession
    ):
        rows = (
            await db.execute(
                select(Role.name, Permission.name)
                .select_from(RolePermission)
                .join(Role, Role.id == RolePermission.role_id)
                .join(Permission, Permission.id == RolePermission.permission_id)
            )
        ).all()
        granted: dict[str, set[str]] = {}
        for role_name, permission_name in rows:
            granted.setdefault(role_name, set()).add(permission_name)

        expected = {
            role.value: {p.value for p in perms}
            for role, perms in ROLE_PERMISSIONS.items()
        }
        assert granted == expected

    def test_every_permission_is_held_by_at_least_one_role(self):
        held = {
            permission.value
            for permissions in ROLE_PERMISSIONS.values()
            for permission in permissions
        }
        assert held == {p.value for p in PermissionEnum}

    def test_the_owner_holds_everything(self):
        """The owner is the backstop, including for permissions added later."""
        assert set(ROLE_PERMISSIONS[RoleEnum.OWNER]) == set(PermissionEnum)

    @pytest.mark.asyncio
    async def test_the_permissions_endpoint_reports_the_catalogue(
        self, owner_client
    ):
        response = await owner_client.get("/api/v1/auth/permissions")
        assert response.status_code == 200
        assert set(response.json()["permissions"]) == {p.value for p in PermissionEnum}


class TestRoutePermissions:
    def test_every_route_permission_is_in_the_catalogue(self):
        unknown = {
            f"{prefix}{path}: {permission.value}"
            for prefix, path, route in _all_routes()
            for permission in _route_permissions(route)
            if permission not in set(PermissionEnum)
        }
        assert unknown == set()

    def test_every_route_requires_at_least_one_permission(self):
        """An unguarded route is a route anybody with a token can call.

        Only the health check and the auth endpoints are meant to be open, and
        neither is in this list — auth is excluded because signing in cannot
        require a permission the caller does not have yet.
        """
        unguarded = sorted(
            f"{prefix}{path}"
            for prefix, path, route in _all_routes()
            if not _route_permissions(route)
        )
        assert unguarded == []

    def test_the_introspection_can_see_a_permission(self):
        """A guard for the guards.

        If the closure walk stopped finding permissions, every assertion above
        would pass vacuously — and a suite that cannot fail is worse than no
        suite, because it looks like coverage.
        """
        found = [
            _route_permissions(route)
            for prefix, path, route in _all_routes()
            if path == "/" and prefix == "/api/v1/invoices"
        ]
        assert found and PermissionEnum.INVOICES_READ in found[0]


class TestCustomerHeldPermissionsDoNotOpenShopEndpoints:
    """What a customer and the shop have in common, written down.

    A customer holds six permissions that also guard shop-wide endpoints. That is
    not an oversight to be tidied away — it is the reason the shop's routers sit
    behind a staff gate at all. Removing the overlap would mean inventing a second
    permission per domain (``invoices:read_own`` against ``invoices:read``) and
    then keeping two vocabularies in step forever.

    So the overlap is deliberate, the gate is what makes it safe, and this test
    pins the list: a seventh shared permission should be a decision somebody
    makes on purpose, not one that arrives with a feature.
    """

    SHARED_BY_DESIGN = {  # noqa: RUF012 - read-only constant; sharing is the point
        "appointments:read",
        "appointments:write",
        "invoices:read",
        "payments:read",
        "payments:write",
        "vehicles:read",
    }

    def test_the_shared_permissions_are_exactly_these(self):
        customer_held = {p.value for p in ROLE_PERMISSIONS[RoleEnum.CUSTOMER]}
        shared = {
            permission.value
            for prefix, path, route in _all_routes()
            if not prefix.startswith(CUSTOMER_REACHABLE)
            for permission in _route_permissions(route)
            if permission.value in customer_held
        }
        assert shared == self.SHARED_BY_DESIGN

    def test_every_shared_permission_is_used_by_the_portal_too(self):
        """Each shared permission earns its place in both audiences.

        If the portal stopped needing one of these, the customer would be holding
        a permission for no reason — and a permission held for no reason is one
        more thing to explain to the next auditor.
        """
        portal_permissions = {
            permission.value
            for prefix, path, route in _all_routes()
            if prefix.startswith(CUSTOMER_REACHABLE)
            for permission in _route_permissions(route)
        }
        assert portal_permissions >= self.SHARED_BY_DESIGN

    def test_the_customer_permissions_no_route_checks_are_accounted_for(self):
        """A permission no route asks for looks like access control and is not.

        Three of these are honest: the portal shows a vehicle's inspections and
        repair orders inside its vehicle history, which is gated on
        ``vehicles:read``, so the permissions record why the customer may see
        their own work even though the route does not ask for them by name. Two
        are reserved for a feedback module that does not exist. Listing them here
        means adding one is a decision, and forgetting to remove one is a
        question a reader can answer.
        """
        portal_permissions = {
            permission.value
            for prefix, path, route in _all_routes()
            if prefix.startswith(CUSTOMER_REACHABLE)
            for permission in _route_permissions(route)
        }
        held = {p.value for p in ROLE_PERMISSIONS[RoleEnum.CUSTOMER]}
        assert held - portal_permissions == {
            "feedback:read",
            "feedback:write",
            "inspections:read",
            "repair_orders:read",
        }


class TestTheTrackersNumbersAreTrue:
    """'Router registered at /api/v1/portal (13 endpoints)' is a claim about code.

    It was written by hand once and never checked, which is the whole problem: a
    number in a tracker rots quietly. Each claim is compared with the routes the
    application actually serves.
    """

    def _claims(self) -> dict[str, int]:
        text = TRACKER.read_text(encoding="utf-8")
        claims = {}
        for prefix, count in re.findall(
            r"Router registered at `([^`]+)` \((\d+) endpoints?", text
        ):
            claims[prefix] = int(count)
        assert claims, "no endpoint claims found in PROJECT_PROGRESS.md"
        return claims

    def test_every_claimed_endpoint_count_matches_the_application(self):
        schema = app.openapi()
        wrong = {}
        for prefix, claimed in self._claims().items():
            actual = sum(
                1
                for path, operations in schema["paths"].items()
                if path == prefix or path.startswith(f"{prefix}/")
                for _ in operations
            )
            if actual != claimed:
                wrong[prefix] = (claimed, actual)
        assert wrong == {}

    def test_every_claimed_prefix_is_a_real_router(self):
        prefixes = {prefix for _, prefix in ROUTERS}
        assert set(self._claims()) <= prefixes
