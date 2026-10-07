"""Tests for quality control.

Covers the QC attempt lifecycle, the verification checks (work completed, parts
recorded, labor recorded, photos attached), rework after a failed check,
independence of the inspector, and RBAC.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.customers.models import Customer
from app.vehicles.models import Vehicle
from tests.factories import VehicleFactory

QC_URL = "/api/v1/qc/"
REPAIR_ORDERS_URL = "/api/v1/repair_orders/"
LABOR_URL = "/api/v1/labor/"
PART_REQUESTS_URL = "/api/v1/part_requests/"


@pytest.fixture()
async def test_customer(db: AsyncSession):
    customer = Customer(
        first_name="QC",
        last_name="Customer",
        email="qc_test@example.com",
        phone="555-0040",
        preferred_contact="EMAIL",
        customer_status="ACTIVE",
    )
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    return customer


@pytest.fixture()
async def test_vehicle(db: AsyncSession, test_customer: Customer):
    vehicle = VehicleFactory.build(customer_id=test_customer.id)
    db.add(vehicle)
    await db.commit()
    await db.refresh(vehicle)
    return vehicle


@pytest.fixture()
async def technician_user_id(db: AsyncSession) -> str:
    user = (
        await db.execute(select(User).where(User.email == "tech@autofix.demo"))
    ).scalar_one()
    return str(user.id)


async def complete_ro(
    client: AsyncClient,
    customer: Customer,
    vehicle: Vehicle,
    *,
    with_labor: bool = True,
    request_part: str | None = None,
) -> dict:
    """Drive a repair order all the way to COMPLETED.

    ``with_labor`` records a labor entry while the order is still live, since
    labour can only be logged during the work itself. ``request_part`` raises a
    part request on the way through, left PENDING unless the caller stages it.
    """
    create = await client.post(
        REPAIR_ORDERS_URL,
        json={
            "customer_id": str(customer.id),
            "vehicle_id": str(vehicle.id),
            "tasks": [{"description": "Replace pads"}, {"description": "Bleed brakes"}],
        },
    )
    assert create.status_code == 201, create.text
    ro = create.json()

    for status_name in ("APPROVED", "IN_PROGRESS"):
        step = await client.patch(
            f"{REPAIR_ORDERS_URL}{ro['id']}/status", params={"status": status_name}
        )
        assert step.status_code == 200, step.text

    if with_labor:
        labor = await client.post(
            LABOR_URL,
            json={
                "repair_order_id": ro["id"],
                "description": "Brake service",
                "actual_hours": 2.5,
                "hourly_rate": 95.0,
            },
        )
        assert labor.status_code == 201, labor.text

    if request_part:
        raised = await client.post(
            PART_REQUESTS_URL,
            json={
                "repair_order_id": ro["id"],
                "part_name": request_part,
                "reason": "Worn beyond service",
            },
        )
        assert raised.status_code == 201, raised.text

    for task in ro["tasks"]:
        done = await client.patch(
            f"{REPAIR_ORDERS_URL}{ro['id']}/tasks/{task['id']}/status",
            params={"status": "COMPLETED"},
        )
        assert done.status_code == 200, done.text

    completed = await client.patch(
        f"{REPAIR_ORDERS_URL}{ro['id']}/status", params={"status": "COMPLETED"}
    )
    assert completed.status_code == 200, completed.text
    return completed.json()


def check_by_type(check: dict, check_type: str) -> dict:
    """Pick one verification line out of a QC response."""
    return next(c for c in check["checks"] if c["check_type"] == check_type)


class TestQualityControlLifecycle:
    @pytest.mark.asyncio
    async def test_cannot_start_before_completion(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """QC only starts once the work is finished."""
        create = await owner_client.post(
            REPAIR_ORDERS_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "tasks": [{"description": "Task"}],
            },
        )
        ro = create.json()
        response = await owner_client.post(
            QC_URL, json={"repair_order_id": ro["id"]}
        )
        assert response.status_code == 400
        assert "COMPLETED" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_start_runs_verification_checks(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Starting QC evaluates every verification against real shop data."""
        ro = await complete_ro(owner_client, test_customer, test_vehicle)
        response = await owner_client.post(
            QC_URL, json={"repair_order_id": ro["id"], "notes": "Final walk-around"}
        )
        assert response.status_code == 201, response.text
        check = response.json()

        assert check["status"] == "IN_PROGRESS"
        assert check["attempt_number"] == 1
        assert check["notes"] == "Final walk-around"
        assert check["inspector_id"] is not None
        assert check["started_at"] is not None
        assert check["passed"] is True

        assert check_by_type(check, "WORK_COMPLETED")["passed"] is True
        assert check_by_type(check, "LABOR_RECORDED")["passed"] is True
        assert check_by_type(check, "PARTS_RECORDED")["passed"] is True
        # Nothing has been photographed yet, and photos are non-blocking.
        photos = check_by_type(check, "PHOTOS_ATTACHED")
        assert photos["passed"] is False
        assert photos["blocking"] is False

        labor = check_by_type(check, "LABOR_RECORDED")
        assert "1 labour record" in labor["evidence"]
        assert "2.50h" in labor["evidence"]

    @pytest.mark.asyncio
    async def test_pass_releases_repair_order(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A pass moves the repair order to QC_PASSED."""
        ro = await complete_ro(owner_client, test_customer, test_vehicle)
        check = (
            await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})
        ).json()

        passed = await owner_client.post(f"{QC_URL}{check['id']}/pass")
        assert passed.status_code == 200, passed.text
        assert passed.json()["status"] == "PASSED"
        assert passed.json()["completed_at"] is not None

        ro_after = await owner_client.get(f"{REPAIR_ORDERS_URL}{ro['id']}")
        assert ro_after.json()["status"] == "QC_PASSED"

    @pytest.mark.asyncio
    async def test_pass_blocked_by_failing_check(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """QC cannot pass while a blocking check fails."""
        ro = await complete_ro(owner_client, test_customer, test_vehicle, with_labor=False)
        check = (
            await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})
        ).json()
        assert check_by_type(check, "LABOR_RECORDED")["passed"] is False
        assert check["passed"] is False

        response = await owner_client.post(f"{QC_URL}{check['id']}/pass")
        assert response.status_code == 400
        assert "LABOR_RECORDED" in response.json()["detail"]

        ro_after = await owner_client.get(f"{REPAIR_ORDERS_URL}{ro['id']}")
        assert ro_after.json()["status"] == "COMPLETED"

    @pytest.mark.asyncio
    async def test_fail_sends_order_back_for_rework(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A failed check returns the repair order to the technician."""
        ro = await complete_ro(owner_client, test_customer, test_vehicle)
        check = (
            await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})
        ).json()

        failed = await owner_client.post(
            f"{QC_URL}{check['id']}/fail", json={"reason": "Pedal travel too long"}
        )
        assert failed.status_code == 200, failed.text
        assert failed.json()["status"] == "FAILED"
        assert failed.json()["failure_reason"] == "Pedal travel too long"

        ro_after = await owner_client.get(f"{REPAIR_ORDERS_URL}{ro['id']}")
        assert ro_after.json()["status"] == "IN_PROGRESS"

    @pytest.mark.asyncio
    async def test_fail_requires_a_reason(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A rework loop with no explanation is refused."""
        ro = await complete_ro(owner_client, test_customer, test_vehicle)
        check = (
            await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})
        ).json()

        response = await owner_client.post(f"{QC_URL}{check['id']}/fail", json={})
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_only_one_open_attempt(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A repair order cannot be inspected twice at once."""
        ro = await complete_ro(owner_client, test_customer, test_vehicle)
        await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})

        response = await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_rework_raises_second_attempt(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """After rework the RO is re-inspected as attempt 2, keeping the history."""
        ro = await complete_ro(owner_client, test_customer, test_vehicle)
        first = (
            await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})
        ).json()
        await owner_client.post(
            f"{QC_URL}{first['id']}/fail", json={"reason": "Air in the line"}
        )

        for task in ro["tasks"]:
            await owner_client.patch(
                f"{REPAIR_ORDERS_URL}{ro['id']}/tasks/{task['id']}/status",
                params={"status": "IN_PROGRESS"},
            )
        for task in ro["tasks"]:
            await owner_client.patch(
                f"{REPAIR_ORDERS_URL}{ro['id']}/tasks/{task['id']}/status",
                params={"status": "COMPLETED"},
            )
        await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro['id']}/status", params={"status": "COMPLETED"}
        )

        second = await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})
        assert second.status_code == 201, second.text
        assert second.json()["attempt_number"] == 2

        history = await owner_client.get(QC_URL, params={"repair_order_id": ro["id"]})
        assert history.json()["meta"]["total"] == 2
        statuses = {c["status"] for c in history.json()["data"]}
        assert statuses == {"FAILED", "IN_PROGRESS"}

    @pytest.mark.asyncio
    async def test_decided_attempt_is_frozen(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """A verdict cannot be revised; the RO moves on."""
        ro = await complete_ro(owner_client, test_customer, test_vehicle)
        check = (
            await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})
        ).json()
        await owner_client.post(f"{QC_URL}{check['id']}/pass")

        again = await owner_client.post(f"{QC_URL}{check['id']}/pass")
        assert again.status_code == 400

        notes = await owner_client.patch(
            f"{QC_URL}{check['id']}", json={"notes": "changed my mind"}
        )
        assert notes.status_code == 400

        override = await owner_client.patch(
            f"{QC_URL}{check['id']}/checks/LABOR_RECORDED", json={"passed": False}
        )
        assert override.status_code == 400

    @pytest.mark.asyncio
    async def test_discard_open_attempt(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """An attempt raised in error can be discarded; a decided one cannot."""
        ro = await complete_ro(owner_client, test_customer, test_vehicle)
        check = (
            await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})
        ).json()

        assert (await owner_client.delete(f"{QC_URL}{check['id']}")).status_code == 204
        assert (
            await owner_client.get(f"{QC_URL}{check['id']}")
        ).status_code == 404

        retry = (
            await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})
        ).json()
        await owner_client.post(f"{QC_URL}{retry['id']}/pass")
        assert (
            await owner_client.delete(f"{QC_URL}{retry['id']}")
        ).status_code == 400


class TestVerificationChecks:
    @pytest.mark.asyncio
    async def test_outstanding_part_request_blocks_pass(
        self, technician_client: AsyncClient, parts_client: AsyncClient, owner_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """A part still waiting on the parts department is a blocking finding."""
        ro = await complete_ro(
            technician_client, test_customer, test_vehicle, request_part="Brake rotor"
        )

        check = (await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})).json()
        parts = check_by_type(check, "PARTS_RECORDED")
        assert parts["passed"] is False
        assert "Brake rotor" in parts["evidence"]

        blocked = await owner_client.post(f"{QC_URL}{check['id']}/pass")
        assert blocked.status_code == 400
        assert "PARTS_RECORDED" in blocked.json()["detail"]

    @pytest.mark.asyncio
    async def test_parts_check_clears_when_parts_are_staged(
        self, technician_client: AsyncClient, parts_client: AsyncClient, owner_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """A fulfilled part request satisfies the parts check."""
        ro = await complete_ro(
            technician_client, test_customer, test_vehicle, request_part="Brake rotor"
        )

        # Stage the part: only *raising* a request needs a live order, so the
        # parts department can still mark an existing one as staged.
        requests = await parts_client.get(PART_REQUESTS_URL, params={"repair_order_id": ro["id"]})
        request_id = requests.json()["data"][0]["id"]
        await parts_client.post(
            f"{PART_REQUESTS_URL}{request_id}/decision", json={"decision": "APPROVED"}
        )
        await parts_client.post(f"{PART_REQUESTS_URL}{request_id}/fulfil")

        check = (await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})).json()
        parts = check_by_type(check, "PARTS_RECORDED")
        assert parts["passed"] is True
        assert parts["evidence"] == "all requested parts staged"

        assert (await owner_client.post(f"{QC_URL}{check['id']}/pass")).status_code == 200

    @pytest.mark.asyncio
    async def test_reverify_picks_up_late_labor(
        self, technician_client: AsyncClient, owner_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """Re-running the automatic checks reflects data recorded since."""
        ro = await complete_ro(technician_client, test_customer, test_vehicle, with_labor=False)
        check = (
            await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})
        ).json()
        assert check_by_type(check, "LABOR_RECORDED")["passed"] is False

        # Rework the order so labour can legally be recorded, then re-inspect.
        await owner_client.post(f"{QC_URL}{check['id']}/fail", json={"reason": "no hours logged"})
        labor = await technician_client.post(
            LABOR_URL,
            json={
                "repair_order_id": ro["id"],
                "description": "Rectification",
                "actual_hours": 1.0,
                "hourly_rate": 95.0,
            },
        )
        assert labor.status_code == 201, labor.text

        # Finish the rework so the order can be inspected again.
        await owner_client.patch(
            f"{REPAIR_ORDERS_URL}{ro['id']}/status", params={"status": "COMPLETED"}
        )
        retry = (await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})).json()
        assert check_by_type(retry, "LABOR_RECORDED")["passed"] is True
        assert retry["attempt_number"] == 2

    @pytest.mark.asyncio
    async def test_inspector_can_override_a_check(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """An override settles a check the shop data cannot answer."""
        ro = await complete_ro(owner_client, test_customer, test_vehicle, with_labor=False)
        check = (
            await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})
        ).json()

        overridden = await owner_client.patch(
            f"{QC_URL}{check['id']}/checks/LABOR_RECORDED",
            json={"passed": True, "notes": "Hours captured on paper"},
        )
        assert overridden.status_code == 200, overridden.text
        item = check_by_type(overridden.json(), "LABOR_RECORDED")
        assert item["passed"] is True
        assert item["auto_verified"] is False
        assert item["notes"] == "Hours captured on paper"

        passed = await owner_client.post(f"{QC_URL}{check['id']}/pass")
        assert passed.status_code == 200

    @pytest.mark.asyncio
    async def test_override_survives_reverify(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Re-verification refreshes automatic checks, not human decisions."""
        ro = await complete_ro(owner_client, test_customer, test_vehicle, with_labor=False)
        check = (
            await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})
        ).json()
        await owner_client.patch(
            f"{QC_URL}{check['id']}/checks/LABOR_RECORDED",
            json={"passed": True, "blocking": False},
        )

        reverified = await owner_client.post(f"{QC_URL}{check['id']}/reverify")
        assert reverified.status_code == 200, reverified.text
        item = check_by_type(reverified.json(), "LABOR_RECORDED")
        assert item["passed"] is True
        assert item["blocking"] is False
        assert item["auto_verified"] is False
        # Automatic lines still get refreshed.
        assert check_by_type(reverified.json(), "WORK_COMPLETED")["auto_verified"] is True

    @pytest.mark.asyncio
    async def test_unknown_check_type_rejected(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Only the defined verification types can be overridden."""
        ro = await complete_ro(owner_client, test_customer, test_vehicle)
        check = (
            await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})
        ).json()
        response = await owner_client.patch(
            f"{QC_URL}{check['id']}/checks/VIBES", json={"passed": True}
        )
        assert response.status_code == 400


class TestQCDocumentation:
    @pytest.mark.asyncio
    async def test_adding_photo_flips_photo_check(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Attaching a photo satisfies the documentation check."""
        ro = await complete_ro(owner_client, test_customer, test_vehicle)
        check = (
            await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})
        ).json()
        assert check_by_type(check, "PHOTOS_ATTACHED")["passed"] is False

        with_photo = await owner_client.post(
            f"{QC_URL}{check['id']}/photos",
            json={"photo_url": "https://cdn.autofix/qc/1.jpg", "caption": "Front pads"},
        )
        assert with_photo.status_code == 200, with_photo.text
        photos = with_photo.json()["photos"]
        assert len(photos) == 1
        assert photos[0]["caption"] == "Front pads"

        item = check_by_type(with_photo.json(), "PHOTOS_ATTACHED")
        assert item["passed"] is True
        assert "1 QC photo" in item["evidence"]

        without = await owner_client.delete(
            f"{QC_URL}{check['id']}/photos/{photos[0]['id']}"
        )
        assert without.status_code == 200, without.text
        assert without.json()["photos"] == []
        assert check_by_type(without.json(), "PHOTOS_ATTACHED")["passed"] is False

    @pytest.mark.asyncio
    async def test_inspection_photos_count_as_documentation(
        self, owner_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Photos taken during the vehicle inspection document the work."""
        inspection = await owner_client.post(
            "/api/v1/inspections/",
            json={"vehicle_id": str(test_vehicle.id), "customer_id": str(test_customer.id)},
        )
        assert inspection.status_code == 201, inspection.text
        inspection_id = inspection.json()["id"]
        item = await owner_client.post(
            f"/api/v1/inspections/{inspection_id}/items",
            json={
                "category": "Brakes",
                "item_name": "Front pads",
                "status": "ATTENTION",
                "photo_url": "https://cdn.autofix/inspection/pad.jpg",
                "photo_caption": "Worn pad",
            },
        )
        assert item.status_code == 201, item.text

        ro = await complete_ro(owner_client, test_customer, test_vehicle)
        check = (await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})).json()
        photo_check = check_by_type(check, "PHOTOS_ATTACHED")
        assert photo_check["passed"] is True
        assert "1 inspection photo" in photo_check["evidence"]


class TestQCQueue:
    @pytest.mark.asyncio
    async def test_queue_lists_completed_orders_awaiting_qc(
        self, technician_client: AsyncClient, owner_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """Completed work shows up in the QC queue until it is inspected."""
        ro = await complete_ro(technician_client, test_customer, test_vehicle)

        queue = await owner_client.get(f"{QC_URL}queue")
        assert queue.status_code == 200, queue.text
        entries = queue.json()
        assert [e["repair_order_id"] for e in entries] == [ro["id"]]
        assert entries[0]["has_open_check"] is False

        check = (await owner_client.post(QC_URL, json={"repair_order_id": ro["id"]})).json()
        entries = (await owner_client.get(f"{QC_URL}queue")).json()
        assert entries[0]["has_open_check"] is True

        await owner_client.post(f"{QC_URL}{check['id']}/pass")
        entries = (await owner_client.get(f"{QC_URL}queue")).json()
        assert entries == []

    @pytest.mark.asyncio
    async def test_queue_excludes_in_progress_orders(
        self, technician_client: AsyncClient, owner_client: AsyncClient,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """Only COMPLETED orders are waiting on quality control."""
        await technician_client.post(
            REPAIR_ORDERS_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "tasks": [{"description": "Task"}],
            },
        )
        queue = await owner_client.get(f"{QC_URL}queue")
        assert queue.json() == []


class TestQCIndependence:
    @pytest.mark.asyncio
    async def test_technician_cannot_inspect_own_work(
        self, technician_client: AsyncClient, technician_user_id: str,
        test_customer: Customer, test_vehicle: Vehicle,
    ):
        """Self-inspection is refused: it would make the gate decorative."""
        create = await technician_client.post(
            REPAIR_ORDERS_URL,
            json={
                "customer_id": str(test_customer.id),
                "vehicle_id": str(test_vehicle.id),
                "technician_id": technician_user_id,
                "tasks": [{"description": "Task"}],
            },
        )
        ro = create.json()
        for status_name in ("APPROVED", "IN_PROGRESS"):
            await technician_client.patch(
                f"{REPAIR_ORDERS_URL}{ro['id']}/status", params={"status": status_name}
            )
        task_id = ro["tasks"][0]["id"]
        await technician_client.patch(
            f"{REPAIR_ORDERS_URL}{ro['id']}/tasks/{task_id}/status",
            params={"status": "COMPLETED"},
        )
        await technician_client.patch(
            f"{REPAIR_ORDERS_URL}{ro['id']}/status", params={"status": "COMPLETED"}
        )

        response = await technician_client.post(
            QC_URL, json={"repair_order_id": ro["id"]}
        )
        assert response.status_code == 400
        assert "cannot run quality control" in response.json()["detail"]


class TestQCRbac:
    @pytest.mark.asyncio
    async def test_advisor_can_perform_qc(
        self, manager_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Service advisors hold qc:perform."""
        ro = await complete_ro(manager_client, test_customer, test_vehicle)
        response = await manager_client.post(QC_URL, json={"repair_order_id": ro["id"]})
        assert response.status_code == 201, response.text

    @pytest.mark.asyncio
    async def test_technician_can_read_qc_history(
        self, technician_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Technicians hold qc:read."""
        response = await technician_client.get(QC_URL)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_parts_staff_cannot_perform_qc(
        self, parts_client: AsyncClient, test_customer: Customer, test_vehicle: Vehicle
    ):
        """Parts staff hold neither qc:read nor qc:perform."""
        assert (await parts_client.get(QC_URL)).status_code == 403
        response = await parts_client.post(
            QC_URL, json={"repair_order_id": "00000000-0000-0000-0000-000000000000"}
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_customer_denied(self, customer_client: AsyncClient):
        """Customers hold no QC permissions."""
        assert (await customer_client.get(QC_URL)).status_code == 403
        assert (await customer_client.get(f"{QC_URL}queue")).status_code == 403

    @pytest.mark.asyncio
    async def test_unauthenticated_denied(self, unauth_client: AsyncClient):
        """Anonymous callers are rejected."""
        assert (await unauth_client.get(QC_URL)).status_code == 401
