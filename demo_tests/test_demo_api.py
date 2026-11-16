from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.demo_api import app, reset_demo_state


@pytest.fixture
async def client():
    reset_demo_state()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://demo.test") as session:
        yield session


async def sign_in(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "demo1234"},
    )
    assert response.status_code == 200
    tokens = response.json()
    return {"Authorization": "Bearer " + tokens["access_token"]}


@pytest.mark.asyncio
async def test_health_and_demo_auth(client: AsyncClient):
    health = await client.get("/health")
    assert health.status_code == 200
    assert health.json()["storage"] == "in-memory"

    headers = await sign_in(client, "owner@autofix.demo")
    profile = await client.get("/api/v1/auth/me", headers=headers)
    permissions = await client.get("/api/v1/auth/permissions", headers=headers)

    assert profile.json()["roles"] == ["OWNER"]
    assert "customers:read" in permissions.json()["permissions"]
    assert "reports:analytics" in permissions.json()["permissions"]


@pytest.mark.asyncio
async def test_shop_records_and_repair_task_transition(client: AsyncClient):
    headers = await sign_in(client, "owner@autofix.demo")

    customers = await client.get("/api/v1/customers/?page=1&size=2", headers=headers)
    assert customers.status_code == 200
    assert customers.json()["meta"]["total"] == 4
    assert len(customers.json()["data"]) == 2

    before = await client.get("/api/v1/repair_orders/repair-1", headers=headers)
    assert before.json()["tasks"][1]["status"] == "PENDING"

    changed = await client.patch(
        "/api/v1/repair_orders/repair-1/tasks/task-2/status",
        headers=headers,
        json={"status": "COMPLETED"},
    )
    assert changed.status_code == 200
    assert changed.json()["status"] == "COMPLETED"

    after = await client.get("/api/v1/repair_orders/repair-1", headers=headers)
    assert after.json()["all_tasks_done"] is True


@pytest.mark.asyncio
async def test_customer_portal_is_scoped_and_mutations_are_in_memory(client: AsyncClient):
    headers = await sign_in(client, "customer@autofix.demo")

    summary = await client.get("/api/v1/portal/", headers=headers)
    assert summary.status_code == 200
    assert summary.json()["full_name"] == "David Wilson"
    assert summary.json()["vehicle_count"] == 1

    request = await client.post(
        "/api/v1/portal/service-requests",
        headers=headers,
        json={"title": "Demo AC inspection", "priority": "HIGH", "vehicle_id": "vehicle-1"},
    )
    assert request.status_code == 200
    assert request.json()["status"] == "NEW"

    portal_requests = await client.get("/api/v1/portal/service-requests", headers=headers)
    assert any(item["title"] == "Demo AC inspection" for item in portal_requests.json())

    payment = await client.post(
        "/api/v1/portal/invoices/invoice-1/payments",
        headers=headers,
        json={"amount": 100, "method": "CARD"},
    )
    assert payment.status_code == 200
    assert payment.json()["invoice_balance"] == 345.2


@pytest.mark.asyncio
async def test_customer_cannot_open_staff_collections(client: AsyncClient):
    headers = await sign_in(client, "customer@autofix.demo")

    response = await client.get("/api/v1/customers/", headers=headers)
    assert response.status_code == 403

    own_vehicles = await client.get("/api/v1/portal/vehicles", headers=headers)
    assert own_vehicles.status_code == 200
    assert len(own_vehicles.json()) == 1


@pytest.mark.asyncio
async def test_state_resets_to_seed_data(client: AsyncClient):
    headers = await sign_in(client, "owner@autofix.demo")
    await client.post(
        "/api/v1/customers/",
        headers=headers,
        json={"first_name": "Demo", "last_name": "Visitor", "email": "visitor@example.test"},
    )

    reset_demo_state()
    fresh_headers = await sign_in(client, "owner@autofix.demo")
    response = await client.get("/api/v1/customers/", headers=fresh_headers)

    assert response.json()["meta"]["total"] == 4
