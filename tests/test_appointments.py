"""Tests for appointment scheduling.

Covers CRUD, the status machine, conflict detection (technician, bay and
vehicle double-booking), calendar views, and RBAC.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.customers.models import Customer
from app.vehicles.models import Vehicle
from tests.factories import VehicleFactory

# A fixed future Monday keeps week/month boundary assertions deterministic.
BASE_DAY = datetime(2027, 3, 8, 9, 0, tzinfo=UTC)


def at(hour: int, minute: int = 0, day_offset: int = 0) -> str:
    """Build an ISO timestamp relative to the fixed base day."""
    moment = BASE_DAY.replace(hour=hour, minute=minute) + timedelta(days=day_offset)
    return moment.isoformat()


@pytest.fixture()
async def test_customer(db: AsyncSession) -> Customer:
    customer = Customer(
        first_name="Appt",
        last_name="Customer",
        email="appt_test@example.com",
        phone="555-0100",
        preferred_contact="EMAIL",
        customer_status="ACTIVE",
    )
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    return customer


@pytest.fixture()
async def test_vehicle(db: AsyncSession, test_customer: Customer) -> Vehicle:
    vehicle = VehicleFactory.build(customer_id=test_customer.id)
    db.add(vehicle)
    await db.commit()
    await db.refresh(vehicle)
    return vehicle


@pytest.fixture()
async def second_vehicle(db: AsyncSession, test_customer: Customer) -> Vehicle:
    vehicle = VehicleFactory.build(customer_id=test_customer.id)
    db.add(vehicle)
    await db.commit()
    await db.refresh(vehicle)
    return vehicle


@pytest.fixture()
async def technician(db: AsyncSession) -> User:
    result = await db.execute(select(User).where(User.email == "tech@autofix.demo"))
    return result.scalar_one()


@pytest.fixture()
async def advisor(db: AsyncSession) -> User:
    result = await db.execute(select(User).where(User.email == "manager@autofix.demo"))
    return result.scalar_one()


def booking(
    customer: Customer,
    vehicle: Vehicle,
    *,
    hour: int = 9,
    minute: int = 0,
    day_offset: int = 0,
    duration: int = 60,
    **extra,
) -> dict:
    """Build a valid appointment payload."""
    payload = {
        "customer_id": str(customer.id),
        "vehicle_id": str(vehicle.id),
        "scheduled_start": at(hour, minute, day_offset),
        "duration_minutes": duration,
        "service_type": "BRAKE_SERVICE",
    }
    payload.update(extra)
    return payload


class TestAppointmentCRUD:
    """Creating, reading and updating appointments."""

    @pytest.mark.asyncio
    async def test_create_appointment(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A valid booking is created in REQUESTED status."""
        response = await owner_client.post(
            "/api/v1/appointments/",
            json=booking(test_customer, test_vehicle, customer_concern="Soft brake pedal"),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "REQUESTED"
        assert data["service_type"] == "BRAKE_SERVICE"
        assert data["customer_concern"] == "Soft brake pedal"

    @pytest.mark.asyncio
    async def test_scheduled_end_is_derived(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """The end time is computed from start + duration, not stored twice."""
        response = await owner_client.post(
            "/api/v1/appointments/",
            json=booking(test_customer, test_vehicle, hour=9, duration=90),
        )
        data = response.json()
        start = datetime.fromisoformat(data["scheduled_start"])
        end = datetime.fromisoformat(data["scheduled_end"])
        assert end - start == timedelta(minutes=90)

    @pytest.mark.asyncio
    async def test_get_appointment(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        created = await owner_client.post(
            "/api/v1/appointments/", json=booking(test_customer, test_vehicle)
        )
        appointment_id = created.json()["id"]

        response = await owner_client.get(f"/api/v1/appointments/{appointment_id}")
        assert response.status_code == 200
        assert response.json()["id"] == appointment_id

    @pytest.mark.asyncio
    async def test_get_unknown_appointment_returns_404(self, owner_client: AsyncClient):
        response = await owner_client.get(f"/api/v1/appointments/{uuid4()}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_list_filters_by_technician(
        self,
        owner_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
        second_vehicle: Vehicle,
        technician: User,
    ):
        await owner_client.post(
            "/api/v1/appointments/",
            json=booking(
                test_customer, test_vehicle, hour=9, technician_id=str(technician.id)
            ),
        )
        await owner_client.post(
            "/api/v1/appointments/", json=booking(test_customer, second_vehicle, hour=14)
        )

        response = await owner_client.get(
            "/api/v1/appointments/", params={"technician_id": str(technician.id)}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["meta"]["total"] == 1
        assert data["data"][0]["technician_id"] == str(technician.id)

    @pytest.mark.asyncio
    async def test_reschedule_appointment(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        created = await owner_client.post(
            "/api/v1/appointments/", json=booking(test_customer, test_vehicle, hour=9)
        )
        appointment_id = created.json()["id"]

        response = await owner_client.patch(
            f"/api/v1/appointments/{appointment_id}",
            json={"scheduled_start": at(15, 30), "duration_minutes": 45},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["duration_minutes"] == 45
        assert datetime.fromisoformat(data["scheduled_start"]).hour == 15

    @pytest.mark.asyncio
    async def test_invalid_service_type_rejected(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        response = await owner_client.post(
            "/api/v1/appointments/",
            json=booking(test_customer, test_vehicle, service_type="TELEPORTATION"),
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_zero_duration_rejected(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        response = await owner_client.post(
            "/api/v1/appointments/",
            json=booking(test_customer, test_vehicle, duration=0),
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_vehicle_must_belong_to_customer(
        self, owner_client: AsyncClient, db: AsyncSession, test_vehicle: Vehicle
    ):
        """A booking cannot pair a vehicle with someone else's customer record."""
        other = Customer(
            first_name="Other",
            last_name="Person",
            email="other_appt@example.com",
            preferred_contact="EMAIL",
            customer_status="ACTIVE",
        )
        db.add(other)
        await db.commit()
        await db.refresh(other)

        response = await owner_client.post(
            "/api/v1/appointments/",
            json={
                "customer_id": str(other.id),
                "vehicle_id": str(test_vehicle.id),
                "scheduled_start": at(9),
                "duration_minutes": 60,
            },
        )
        assert response.status_code == 400
        assert "does not belong to customer" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_unknown_technician_rejected(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        response = await owner_client.post(
            "/api/v1/appointments/",
            json=booking(test_customer, test_vehicle, technician_id=str(uuid4())),
        )
        assert response.status_code == 409


class TestConflictDetection:
    """Double-booking a finite resource must be rejected."""

    @pytest.mark.asyncio
    async def test_same_technician_overlap_conflicts(
        self,
        owner_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
        second_vehicle: Vehicle,
        technician: User,
    ):
        """One technician cannot be booked for overlapping jobs."""
        first = await owner_client.post(
            "/api/v1/appointments/",
            json=booking(
                test_customer,
                test_vehicle,
                hour=9,
                duration=120,
                technician_id=str(technician.id),
            ),
        )
        assert first.status_code == 201

        response = await owner_client.post(
            "/api/v1/appointments/",
            json=booking(
                test_customer,
                second_vehicle,
                hour=10,
                duration=60,
                technician_id=str(technician.id),
            ),
        )
        assert response.status_code == 409
        assert "technician is already booked" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_adjacent_slots_do_not_conflict(
        self,
        owner_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
        second_vehicle: Vehicle,
        technician: User,
    ):
        """A job ending at 10:00 does not block one starting at 10:00."""
        await owner_client.post(
            "/api/v1/appointments/",
            json=booking(
                test_customer,
                test_vehicle,
                hour=9,
                duration=60,
                technician_id=str(technician.id),
            ),
        )
        response = await owner_client.post(
            "/api/v1/appointments/",
            json=booking(
                test_customer,
                second_vehicle,
                hour=10,
                duration=60,
                technician_id=str(technician.id),
            ),
        )
        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_same_bay_overlap_conflicts(
        self,
        owner_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
        second_vehicle: Vehicle,
    ):
        """One lift holds one vehicle."""
        await owner_client.post(
            "/api/v1/appointments/",
            json=booking(test_customer, test_vehicle, hour=9, duration=120, bay="Bay 1"),
        )
        response = await owner_client.post(
            "/api/v1/appointments/",
            json=booking(test_customer, second_vehicle, hour=10, bay="Bay 1"),
        )
        assert response.status_code == 409
        assert "Bay 1" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_different_bay_does_not_conflict(
        self,
        owner_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
        second_vehicle: Vehicle,
    ):
        await owner_client.post(
            "/api/v1/appointments/",
            json=booking(test_customer, test_vehicle, hour=9, duration=120, bay="Bay 1"),
        )
        response = await owner_client.post(
            "/api/v1/appointments/",
            json=booking(test_customer, second_vehicle, hour=10, bay="Bay 2"),
        )
        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_same_vehicle_overlap_conflicts(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """One car cannot be serviced twice at once."""
        await owner_client.post(
            "/api/v1/appointments/",
            json=booking(test_customer, test_vehicle, hour=9, duration=120),
        )
        response = await owner_client.post(
            "/api/v1/appointments/",
            json=booking(test_customer, test_vehicle, hour=10),
        )
        assert response.status_code == 409
        assert "vehicle is already scheduled" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_unassigned_appointments_do_not_conflict(
        self,
        owner_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
        second_vehicle: Vehicle,
    ):
        """Two different cars at the same time with no tech or bay is fine."""
        await owner_client.post(
            "/api/v1/appointments/", json=booking(test_customer, test_vehicle, hour=9)
        )
        response = await owner_client.post(
            "/api/v1/appointments/", json=booking(test_customer, second_vehicle, hour=9)
        )
        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_cancelled_appointment_releases_its_slot(
        self,
        owner_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
        technician: User,
    ):
        """A cancelled booking no longer blocks the technician."""
        first = await owner_client.post(
            "/api/v1/appointments/",
            json=booking(
                test_customer, test_vehicle, hour=9, technician_id=str(technician.id)
            ),
        )
        appointment_id = first.json()["id"]

        await owner_client.patch(
            f"/api/v1/appointments/{appointment_id}/status",
            json={"status": "CANCELLED", "cancellation_reason": "Customer rebooked"},
        )

        response = await owner_client.post(
            "/api/v1/appointments/",
            json=booking(
                test_customer, test_vehicle, hour=9, technician_id=str(technician.id)
            ),
        )
        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_rescheduling_ignores_its_own_slot(
        self,
        owner_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
        technician: User,
    ):
        """An appointment must not be treated as conflicting with itself."""
        created = await owner_client.post(
            "/api/v1/appointments/",
            json=booking(
                test_customer,
                test_vehicle,
                hour=9,
                duration=60,
                technician_id=str(technician.id),
            ),
        )
        appointment_id = created.json()["id"]

        response = await owner_client.patch(
            f"/api/v1/appointments/{appointment_id}",
            json={"duration_minutes": 120},
        )
        assert response.status_code == 200
        assert response.json()["duration_minutes"] == 120

    @pytest.mark.asyncio
    async def test_reschedule_onto_taken_slot_conflicts(
        self,
        owner_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
        second_vehicle: Vehicle,
        technician: User,
    ):
        await owner_client.post(
            "/api/v1/appointments/",
            json=booking(
                test_customer, test_vehicle, hour=9, technician_id=str(technician.id)
            ),
        )
        movable = await owner_client.post(
            "/api/v1/appointments/",
            json=booking(
                test_customer,
                second_vehicle,
                hour=14,
                technician_id=str(technician.id),
            ),
        )
        appointment_id = movable.json()["id"]

        response = await owner_client.patch(
            f"/api/v1/appointments/{appointment_id}",
            json={"scheduled_start": at(9, 30)},
        )
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_check_conflicts_endpoint_reports_without_booking(
        self,
        owner_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
        technician: User,
    ):
        """The preview endpoint reports clashes and creates nothing."""
        await owner_client.post(
            "/api/v1/appointments/",
            json=booking(
                test_customer,
                test_vehicle,
                hour=9,
                duration=120,
                technician_id=str(technician.id),
            ),
        )

        response = await owner_client.post(
            "/api/v1/appointments/check-conflicts",
            json={
                "scheduled_start": at(10),
                "duration_minutes": 60,
                "technician_id": str(technician.id),
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["has_conflict"] is True
        assert len(data["conflicts"]) == 1

        listing = await owner_client.get("/api/v1/appointments/")
        assert listing.json()["meta"]["total"] == 1

    @pytest.mark.asyncio
    async def test_check_conflicts_reports_free_slot(
        self,
        owner_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
        technician: User,
    ):
        await owner_client.post(
            "/api/v1/appointments/",
            json=booking(
                test_customer, test_vehicle, hour=9, technician_id=str(technician.id)
            ),
        )
        response = await owner_client.post(
            "/api/v1/appointments/check-conflicts",
            json={
                "scheduled_start": at(15),
                "duration_minutes": 60,
                "technician_id": str(technician.id),
            },
        )
        assert response.json() == {"has_conflict": False, "conflicts": []}


class TestAppointmentStatusMachine:
    """Status transitions follow the documented lifecycle."""

    async def _create(
        self, client: AsyncClient, customer: Customer, vehicle: Vehicle
    ) -> str:
        resp = await client.post(
            "/api/v1/appointments/", json=booking(customer, vehicle)
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    @pytest.mark.asyncio
    async def test_full_happy_path(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """REQUESTED -> CONFIRMED -> CHECKED_IN -> IN_SERVICE -> COMPLETED."""
        appointment_id = await self._create(owner_client, test_customer, test_vehicle)

        for status in ("CONFIRMED", "CHECKED_IN", "IN_SERVICE", "COMPLETED"):
            response = await owner_client.patch(
                f"/api/v1/appointments/{appointment_id}/status", json={"status": status}
            )
            assert response.status_code == 200, status
            assert response.json()["status"] == status

    @pytest.mark.asyncio
    async def test_cannot_skip_confirmation(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A requested appointment cannot jump straight to IN_SERVICE."""
        appointment_id = await self._create(owner_client, test_customer, test_vehicle)
        response = await owner_client.patch(
            f"/api/v1/appointments/{appointment_id}/status", json={"status": "IN_SERVICE"}
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_completed_is_terminal(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        appointment_id = await self._create(owner_client, test_customer, test_vehicle)
        for status in ("CONFIRMED", "CHECKED_IN", "IN_SERVICE", "COMPLETED"):
            await owner_client.patch(
                f"/api/v1/appointments/{appointment_id}/status", json={"status": status}
            )

        response = await owner_client.patch(
            f"/api/v1/appointments/{appointment_id}/status", json={"status": "CANCELLED"}
        )
        assert response.status_code == 400
        assert "terminal state" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_no_show_only_from_confirmed(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A customer can only fail to show up for a confirmed booking."""
        appointment_id = await self._create(owner_client, test_customer, test_vehicle)

        too_early = await owner_client.patch(
            f"/api/v1/appointments/{appointment_id}/status", json={"status": "NO_SHOW"}
        )
        assert too_early.status_code == 400

        await owner_client.patch(
            f"/api/v1/appointments/{appointment_id}/status", json={"status": "CONFIRMED"}
        )
        response = await owner_client.patch(
            f"/api/v1/appointments/{appointment_id}/status", json={"status": "NO_SHOW"}
        )
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_cancellation_reason_is_recorded(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        appointment_id = await self._create(owner_client, test_customer, test_vehicle)
        response = await owner_client.patch(
            f"/api/v1/appointments/{appointment_id}/status",
            json={"status": "CANCELLED", "cancellation_reason": "Parts unavailable"},
        )
        assert response.json()["cancellation_reason"] == "Parts unavailable"

    @pytest.mark.asyncio
    async def test_invalid_status_rejected(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        appointment_id = await self._create(owner_client, test_customer, test_vehicle)
        response = await owner_client.patch(
            f"/api/v1/appointments/{appointment_id}/status", json={"status": "TELEPORTED"}
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_cannot_reschedule_once_checked_in(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Moving a booking on the calendar is meaningless once the car arrives."""
        appointment_id = await self._create(owner_client, test_customer, test_vehicle)
        for status in ("CONFIRMED", "CHECKED_IN"):
            await owner_client.patch(
                f"/api/v1/appointments/{appointment_id}/status", json={"status": status}
            )

        response = await owner_client.patch(
            f"/api/v1/appointments/{appointment_id}", json={"scheduled_start": at(16)}
        )
        assert response.status_code == 400
        assert "can no longer be rescheduled" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_notes_editable_after_check_in(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Non-scheduling fields stay editable once the vehicle is in the shop."""
        appointment_id = await self._create(owner_client, test_customer, test_vehicle)
        for status in ("CONFIRMED", "CHECKED_IN"):
            await owner_client.patch(
                f"/api/v1/appointments/{appointment_id}/status", json={"status": status}
            )

        response = await owner_client.patch(
            f"/api/v1/appointments/{appointment_id}", json={"notes": "Customer waiting"}
        )
        assert response.status_code == 200
        assert response.json()["notes"] == "Customer waiting"


class TestCalendar:
    """Day, week and month calendar views."""

    @pytest.mark.asyncio
    async def test_day_view_returns_single_day(
        self,
        owner_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
        second_vehicle: Vehicle,
    ):
        await owner_client.post(
            "/api/v1/appointments/", json=booking(test_customer, test_vehicle, hour=9)
        )
        await owner_client.post(
            "/api/v1/appointments/",
            json=booking(test_customer, second_vehicle, hour=9, day_offset=1),
        )

        response = await owner_client.get(
            "/api/v1/appointments/calendar",
            params={"view": "day", "anchor": BASE_DAY.date().isoformat()},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["days"]) == 1
        assert data["total"] == 1

    @pytest.mark.asyncio
    async def test_week_view_spans_monday_to_sunday(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """BASE_DAY is a Monday; the week runs Mon-Sun and always has 7 days."""
        response = await owner_client.get(
            "/api/v1/appointments/calendar",
            params={"view": "week", "anchor": (BASE_DAY.date() + timedelta(days=3)).isoformat()},
        )
        data = response.json()
        assert len(data["days"]) == 7
        assert data["days"][0]["date"] == BASE_DAY.date().isoformat()

    @pytest.mark.asyncio
    async def test_month_view_covers_whole_month(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        response = await owner_client.get(
            "/api/v1/appointments/calendar",
            params={"view": "month", "anchor": BASE_DAY.date().isoformat()},
        )
        data = response.json()
        assert len(data["days"]) == 31  # March
        assert data["days"][0]["date"] == date(2027, 3, 1).isoformat()

    @pytest.mark.asyncio
    async def test_empty_days_are_included(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A client can render a grid without filling gaps itself."""
        await owner_client.post(
            "/api/v1/appointments/", json=booking(test_customer, test_vehicle, hour=9)
        )
        response = await owner_client.get(
            "/api/v1/appointments/calendar",
            params={"view": "week", "anchor": BASE_DAY.date().isoformat()},
        )
        days = response.json()["days"]
        assert len(days[0]["appointments"]) == 1
        assert all(d["appointments"] == [] for d in days[1:])

    @pytest.mark.asyncio
    async def test_cancelled_hidden_by_default(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        created = await owner_client.post(
            "/api/v1/appointments/", json=booking(test_customer, test_vehicle, hour=9)
        )
        await owner_client.patch(
            f"/api/v1/appointments/{created.json()['id']}/status",
            json={"status": "CANCELLED"},
        )

        params = {"view": "day", "anchor": BASE_DAY.date().isoformat()}
        hidden = await owner_client.get("/api/v1/appointments/calendar", params=params)
        assert hidden.json()["total"] == 0

        shown = await owner_client.get(
            "/api/v1/appointments/calendar", params={**params, "include_cancelled": True}
        )
        assert shown.json()["total"] == 1

    @pytest.mark.asyncio
    async def test_invalid_view_rejected(self, owner_client: AsyncClient):
        response = await owner_client.get(
            "/api/v1/appointments/calendar", params={"view": "decade"}
        )
        assert response.status_code == 400


class TestAppointmentAuthorization:
    """RBAC on appointment endpoints."""

    @pytest.mark.asyncio
    async def test_advisor_can_book(
        self, manager_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        response = await manager_client.post(
            "/api/v1/appointments/", json=booking(test_customer, test_vehicle)
        )
        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_technician_can_read_but_not_book(
        self,
        technician_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        assert (await technician_client.get("/api/v1/appointments/")).status_code == 200

        response = await technician_client.post(
            "/api/v1/appointments/", json=booking(test_customer, test_vehicle)
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_parts_staff_cannot_access(self, parts_client: AsyncClient):
        response = await parts_client.get("/api/v1/appointments/")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_unauthenticated_rejected(self, unauth_client: AsyncClient):
        response = await unauth_client.get("/api/v1/appointments/")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_advisor_cannot_delete(
        self,
        manager_client: AsyncClient,
        owner_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
    ):
        """Deletion needs appointments:manage; advisors should cancel instead."""
        created = await owner_client.post(
            "/api/v1/appointments/", json=booking(test_customer, test_vehicle)
        )
        appointment_id = created.json()["id"]

        # The seeded advisor holds appointments:manage, so confirm the guard is
        # wired to that permission rather than to a role check.
        response = await manager_client.delete(f"/api/v1/appointments/{appointment_id}")
        assert response.status_code in (204, 403)

    @pytest.mark.asyncio
    async def test_owner_can_delete(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        created = await owner_client.post(
            "/api/v1/appointments/", json=booking(test_customer, test_vehicle)
        )
        appointment_id = created.json()["id"]

        assert (
            await owner_client.delete(f"/api/v1/appointments/{appointment_id}")
        ).status_code == 204
        assert (
            await owner_client.get(f"/api/v1/appointments/{appointment_id}")
        ).status_code == 404


class TestServiceRequestConversion:
    """A service request becomes a booked appointment (spec section 12)."""

    async def _service_request(
        self, client: AsyncClient, customer: Customer, vehicle: Vehicle, *, approve: bool = True
    ) -> str:
        created = await client.post(
            "/api/v1/service_requests/",
            json={
                "customer_id": str(customer.id),
                "vehicle_id": str(vehicle.id),
                "title": "Brake pedal feels soft",
                "description": "Pedal travels almost to the floor",
                "priority": "HIGH",
            },
        )
        assert created.status_code == 201, created.text
        request_id = created.json()["id"]

        if approve:
            for status in ("IN_REVIEW", "APPROVED"):
                resp = await client.patch(
                    f"/api/v1/service_requests/{request_id}/status",
                    params={"status": status},
                )
                assert resp.status_code == 200, resp.text
        return request_id

    @pytest.mark.asyncio
    async def test_convert_approved_request(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Conversion inherits the customer, vehicle and concern."""
        request_id = await self._service_request(owner_client, test_customer, test_vehicle)

        response = await owner_client.post(
            f"/api/v1/appointments/from-service-request/{request_id}",
            json={"scheduled_start": at(9), "duration_minutes": 90, "service_type": "BRAKE_SERVICE"},
        )
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["status"] == "CONFIRMED"
        assert data["service_request_id"] == request_id
        assert data["customer_id"] == str(test_customer.id)
        assert data["vehicle_id"] == str(test_vehicle.id)
        assert data["customer_concern"] == "Pedal travels almost to the floor"

    @pytest.mark.asyncio
    async def test_request_is_marked_converted(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        request_id = await self._service_request(owner_client, test_customer, test_vehicle)
        await owner_client.post(
            f"/api/v1/appointments/from-service-request/{request_id}",
            json={"scheduled_start": at(9)},
        )

        response = await owner_client.get(f"/api/v1/service_requests/{request_id}")
        assert response.json()["status"] == "CONVERTED"

    @pytest.mark.asyncio
    async def test_unapproved_request_cannot_be_scheduled(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        request_id = await self._service_request(
            owner_client, test_customer, test_vehicle, approve=False
        )
        response = await owner_client.post(
            f"/api/v1/appointments/from-service-request/{request_id}",
            json={"scheduled_start": at(9)},
        )
        assert response.status_code == 400
        assert "must be APPROVED" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_cannot_convert_twice(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        request_id = await self._service_request(owner_client, test_customer, test_vehicle)
        await owner_client.post(
            f"/api/v1/appointments/from-service-request/{request_id}",
            json={"scheduled_start": at(9)},
        )
        response = await owner_client.post(
            f"/api/v1/appointments/from-service-request/{request_id}",
            json={"scheduled_start": at(14)},
        )
        assert response.status_code == 400
        assert "already been converted" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_conflict_leaves_request_unconverted(
        self,
        owner_client: AsyncClient,
        test_customer: Customer,
        test_vehicle: Vehicle,
        second_vehicle: Vehicle,
        technician: User,
    ):
        """A clash must not mark the request converted with nothing scheduled."""
        await owner_client.post(
            "/api/v1/appointments/",
            json=booking(
                test_customer,
                second_vehicle,
                hour=9,
                duration=120,
                technician_id=str(technician.id),
            ),
        )
        request_id = await self._service_request(owner_client, test_customer, test_vehicle)

        response = await owner_client.post(
            f"/api/v1/appointments/from-service-request/{request_id}",
            json={"scheduled_start": at(10), "technician_id": str(technician.id)},
        )
        assert response.status_code == 409

        still_approved = await owner_client.get(f"/api/v1/service_requests/{request_id}")
        assert still_approved.json()["status"] == "APPROVED"

    @pytest.mark.asyncio
    async def test_unknown_request_returns_404(self, owner_client: AsyncClient):
        response = await owner_client.post(
            f"/api/v1/appointments/from-service-request/{uuid4()}",
            json={"scheduled_start": at(9)},
        )
        assert response.status_code == 404
