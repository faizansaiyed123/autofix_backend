# ruff: noqa: DTZ011
# The API returns the shop's local calendar date (an order written after
# midnight must not carry yesterday's date because the server runs on UTC), so
# these assertions compare against the same local `date.today()`. Ruff would
# rather they used a UTC date, which would disagree with what the API returns.
"""Tests for suppliers, purchase orders and goods receiving.

Covers the supplier list, raising and editing purchase orders, the order status
machine, the receiving workflow (which must book stock through the inventory
ledger rather than assigning a balance), the low-stock reorder path, supplier and
order summaries, and RBAC.
"""

from __future__ import annotations

import itertools
from datetime import date, timedelta

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.inventory.models import InventoryTransaction, InventoryTransactionType
from app.parts.models import Part
from app.purchase_orders.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
)

SUPPLIERS_URL = "/api/v1/suppliers/"
ORDERS_URL = "/api/v1/purchase_orders/"

SUPPLIER_PAYLOAD = {
    "name": "Bayside Auto Parts",
    "contact_name": "Priya Raman",
    "email": "orders@baysideparts.example",
    "phone": "+1 555 0143",
    "address_line1": "14 Dockside Way",
    "city": "Riverton",
    "state": "CA",
    "postal_code": "90210",
    "account_number": "AF-4471",
    "lead_time_days": 3,
    "payment_terms": "Net 30",
}

PART_PAYLOAD = {
    "part_number": "BOS0986A",
    "sku": "AF-BR-0001",
    "name": "Brake pad set, front",
    "category": "Brakes",
    "unit_cost": 42.50,
    "unit_price": 79.99,
    "reorder_level": 4,
}


# --- helpers ---------------------------------------------------------------

# Catalog codes are unique, so a helper that invents a part for every order needs
# fresh ones each time. Tests that assert on a specific part number pass it in
# explicitly instead.
_PART_SEQ = itertools.count(1)


async def create_supplier(client: AsyncClient, **overrides) -> dict:
    """Add a supplier, defaulting to the standard test supplier."""
    response = await client.post(SUPPLIERS_URL, json={**SUPPLIER_PAYLOAD, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


async def create_part(client: AsyncClient, **overrides) -> dict:
    """Add a catalog line. A new part always starts empty."""
    response = await client.post(
        "/api/v1/parts/", json={**PART_PAYLOAD, **overrides}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_order(
    client: AsyncClient,
    supplier_id: str,
    part_ids: list[str] | None = None,
    quantity: float = 10,
    **overrides,
) -> dict:
    """Raise a draft order, defaulting to a single line of ``quantity`` units."""
    if part_ids is None:
        seq = next(_PART_SEQ)
        part = await create_part(
            client,
            part_number=f"ORDER-{seq}",
            sku=f"SKU-ORDER-{seq}",
            name=f"Ordered part {seq}",
        )
        part_ids = [part["id"]]
    body = {
        "supplier_id": supplier_id,
        "items": [
            {"part_id": part_id, "quantity_ordered": quantity, "unit_cost": 40.0}
            for part_id in part_ids
        ],
        **overrides,
    }
    response = await client.post(ORDERS_URL, json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def send_order(client: AsyncClient, order_id: str) -> dict:
    response = await client.post(f"{ORDERS_URL}{order_id}/send")
    assert response.status_code == 200, response.text
    return response.json()


async def receive(
    client: AsyncClient, order_id: str, lines: list[dict]
) -> dict:
    response = await client.post(
        f"{ORDERS_URL}{order_id}/receive", json={"items": lines}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def sent_order(client: AsyncClient, part_id: str | None = None, **kwargs) -> dict:
    """A supplier, an order with one line, sent and ready to receive against."""
    supplier = await create_supplier(client, name="Delivery Co")
    order = await create_order(client, supplier["id"], part_ids=[part_id] if part_id else None, **kwargs)
    return await send_order(client, order["id"])


# --- suppliers -------------------------------------------------------------


class TestSupplierCatalog:
    """Adding, finding, editing and retiring suppliers."""

    async def test_create_supplier(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)

        assert supplier["name"] == "Bayside Auto Parts"
        assert supplier["contact_name"] == "Priya Raman"
        assert supplier["account_number"] == "AF-4471"
        assert supplier["lead_time_days"] == 3
        assert supplier["status"] == "ACTIVE"
        assert supplier["is_active"] is True
        assert supplier["is_preferred"] is False

    async def test_address_is_rendered_on_one_line(self, parts_client: AsyncClient):
        """The joined address omits every field the supplier did not fill in."""
        supplier = await create_supplier(parts_client, country=None, address_line2=None)

        assert supplier["address"] == "14 Dockside Way, Riverton, CA 90210"

    async def test_blank_fields_become_absent(self, parts_client: AsyncClient):
        """A whitespace-only field is not a value, so it is stored as nothing."""
        supplier = await create_supplier(parts_client, city="   ", notes="  ")

        assert supplier["city"] is None
        assert supplier["notes"] is None

    async def test_duplicate_supplier_rejected(self, parts_client: AsyncClient):
        """The same business cannot be entered twice."""
        await create_supplier(parts_client)

        response = await parts_client.post(
            SUPPLIERS_URL, json={**SUPPLIER_PAYLOAD, "account_number": "AF-9999"}
        )

        assert response.status_code == 409
        assert "already exists" in response.json()["detail"]

    async def test_get_by_name_is_case_insensitive(self, parts_client: AsyncClient):
        """A supplier is found however the caller happens to type its name."""
        created = await create_supplier(parts_client, name="Bayside Auto Parts")

        response = await parts_client.get(f"{SUPPLIERS_URL}{created['id']}")

        assert response.status_code == 200
        assert response.json()["name"] == "Bayside Auto Parts"

    async def test_list_is_paginated(self, parts_client: AsyncClient):
        """Page metadata is real, not decorative."""
        for index in range(5):
            await create_supplier(parts_client, name=f"Supplier {index}")

        response = await parts_client.get(SUPPLIERS_URL, params={"page": 1, "size": 2})

        body = response.json()
        assert response.status_code == 200
        assert body["meta"]["total"] == 5
        assert body["meta"]["pages"] == 3
        assert len(body["data"]) == 2

    async def test_search_matches_name_contact_and_account(
        self, parts_client: AsyncClient
    ):
        await create_supplier(parts_client, name="Bayside Auto Parts")
        await create_supplier(parts_client, name="Citywide Tyres", account_number="AF-2210")

        by_name = await parts_client.get(SUPPLIERS_URL, params={"search": "bayside"})
        by_account = await parts_client.get(SUPPLIERS_URL, params={"search": "AF-2210"})

        assert by_name.json()["meta"]["total"] == 1
        assert by_name.json()["data"][0]["name"] == "Bayside Auto Parts"
        assert by_account.json()["meta"]["total"] == 1
        assert by_account.json()["data"][0]["name"] == "Citywide Tyres"

    async def test_unknown_status_filter_matches_nothing(
        self, parts_client: AsyncClient
    ):
        """A mistyped filter must never silently return the whole list."""
        await create_supplier(parts_client)

        response = await parts_client.get(SUPPLIERS_URL, params={"status": "CLOSED"})

        assert response.status_code == 200
        assert response.json()["meta"]["total"] == 0

    async def test_preferred_filter(self, parts_client: AsyncClient):
        await create_supplier(parts_client, name="Preferred One", is_preferred=True)
        await create_supplier(parts_client, name="Ordinary One")

        response = await parts_client.get(SUPPLIERS_URL, params={"preferred_only": True})

        data = response.json()["data"]
        assert response.json()["meta"]["total"] == 1
        assert data[0]["name"] == "Preferred One"

    async def test_active_endpoint_hides_retired_suppliers(
        self, parts_client: AsyncClient
    ):
        """Order pickers must not offer a supplier the shop stopped buying from."""
        active = await create_supplier(parts_client, name="Still Trading")
        retired = await create_supplier(parts_client, name="Retired Co")
        await parts_client.post(f"{SUPPLIERS_URL}{retired['id']}/deactivate")

        response = await parts_client.get(f"{SUPPLIERS_URL}active")

        names = [s["name"] for s in response.json()]
        assert names == ["Still Trading"]
        assert active["id"] in [s["id"] for s in response.json()]

    async def test_update_supplier(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)

        response = await parts_client.patch(
            f"{SUPPLIERS_URL}{supplier['id']}",
            json={"phone": "+1 555 0900", "is_preferred": True, "notes": "Fast delivery"},
        )

        assert response.status_code == 200
        assert response.json()["phone"] == "+1 555 0900"
        assert response.json()["is_preferred"] is True
        assert response.json()["notes"] == "Fast delivery"

    async def test_update_status_rejects_unknown_value(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)

        response = await parts_client.patch(
            f"{SUPPLIERS_URL}{supplier['id']}", json={"status": "CLOSED"}
        )

        assert response.status_code == 422

    async def test_deactivate_clears_preferred(self, parts_client: AsyncClient):
        """A supplier the shop stopped trading with must leave the shortlist."""
        supplier = await create_supplier(parts_client, is_preferred=True)

        response = await parts_client.post(f"{SUPPLIERS_URL}{supplier['id']}/deactivate")

        assert response.status_code == 200
        assert response.json()["status"] == "INACTIVE"
        assert response.json()["is_active"] is False
        assert response.json()["is_preferred"] is False

    async def test_deactivate_is_idempotent(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)

        first = await parts_client.post(f"{SUPPLIERS_URL}{supplier['id']}/deactivate")
        second = await parts_client.post(f"{SUPPLIERS_URL}{supplier['id']}/deactivate")

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["status"] == "INACTIVE"

    async def test_delete_unused_supplier(self, parts_client: AsyncClient):
        """A supplier nobody has ever ordered from can be removed outright."""
        supplier = await create_supplier(parts_client)

        response = await parts_client.delete(f"{SUPPLIERS_URL}{supplier['id']}")

        assert response.status_code == 204
        assert (await parts_client.get(f"{SUPPLIERS_URL}{supplier['id']}")).status_code == 404

    async def test_delete_supplier_with_orders_refused(self, parts_client: AsyncClient):
        """Past orders are a fact about what was bought, so history is kept."""
        supplier = await create_supplier(parts_client)
        await create_order(parts_client, supplier["id"])

        response = await parts_client.delete(f"{SUPPLIERS_URL}{supplier['id']}")

        assert response.status_code == 400
        assert "INACTIVE" in response.json()["detail"]

    async def test_unknown_supplier_is_404(self, parts_client: AsyncClient):
        response = await parts_client.get(
            f"{SUPPLIERS_URL}2f0b4a1e-0000-4000-8000-000000000000"
        )

        assert response.status_code == 404


class TestSupplierSummary:
    """What the shop has bought from one supplier."""

    async def test_summary_of_a_fresh_supplier(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)

        response = await parts_client.get(f"{SUPPLIERS_URL}{supplier['id']}/summary")

        body = response.json()
        assert response.status_code == 200
        assert body["total_orders"] == 0
        assert body["open_orders"] == 0
        assert body["total_spend"] == 0

    async def test_draft_order_is_open_but_costs_nothing(
        self, parts_client: AsyncClient
    ):
        """An unsent order is not money spent."""
        supplier = await create_supplier(parts_client)
        await create_order(parts_client, supplier["id"], quantity=5)

        body = (await parts_client.get(f"{SUPPLIERS_URL}{supplier['id']}/summary")).json()

        assert body["total_orders"] == 1
        assert body["open_orders"] == 1
        assert body["received_orders"] == 0
        assert body["total_spend"] == 0
        assert body["total_units_received"] == 0

    async def test_received_order_counts_units_and_spend(
        self, parts_client: AsyncClient
    ):
        supplier = await create_supplier(parts_client, name="Delivery Co")
        part = await create_part(parts_client)
        order = await create_order(
            parts_client, supplier["id"], part_ids=[part["id"]], quantity=4
        )
        order = await send_order(parts_client, order["id"])
        await receive(parts_client, order["id"], [{"item_id": order["items"][0]["id"], "quantity": 4}])

        body = (await parts_client.get(f"{SUPPLIERS_URL}{supplier['id']}/summary")).json()

        assert body["received_orders"] == 1
        assert body["total_units_received"] == 4
        # 4 units x 40.00
        assert body["total_spend"] == 160.0
        assert body["last_order_date"] == date.today().isoformat()
        assert body["last_received_at"] is not None

    async def test_cancelled_order_counts_as_neither(
        self, parts_client: AsyncClient
    ):
        """A called-off order is neither open nor received."""
        supplier = await create_supplier(parts_client)
        order = await create_order(parts_client, supplier["id"])

        response = await parts_client.post(f"{ORDERS_URL}{order['id']}/cancel")

        assert response.status_code == 200
        body = (await parts_client.get(f"{SUPPLIERS_URL}{supplier['id']}/summary")).json()
        assert body["total_orders"] == 1
        assert body["open_orders"] == 0
        assert body["received_orders"] == 0


# --- purchase orders --------------------------------------------------------


class TestPurchaseOrderCreation:
    """Raising an order and the money on it."""

    async def test_order_starts_as_a_draft(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)

        order = await create_order(parts_client, supplier["id"])

        assert order["status"] == "DRAFT"
        assert order["is_editable"] is True
        assert order["is_terminal"] is False
        assert order["po_number"].startswith("PO-")
        assert order["order_date"] == date.today().isoformat()
        assert order["supplier_id"] == supplier["id"]

    async def test_po_numbers_are_unique(self, parts_client: AsyncClient):
        """Two orders raised in the same month must not share a number."""
        supplier = await create_supplier(parts_client)

        first = await create_order(parts_client, supplier["id"])
        second = await create_order(parts_client, supplier["id"])

        assert first["po_number"] != second["po_number"]

    async def test_totals_are_computed_from_the_lines(
        self, parts_client: AsyncClient
    ):
        """The header total is derived, so it can never disagree with the lines."""
        supplier = await create_supplier(parts_client)
        first = await create_part(parts_client, part_number="P-1", sku="S-1", name="One")
        second = await create_part(parts_client, part_number="P-2", sku="S-2", name="Two")

        order = await create_order(
            parts_client,
            supplier["id"],
            part_ids=[first["id"], second["id"]],
            quantity=3,
            tax_amount=10.0,
            shipping_amount=5.0,
        )

        # 2 lines x 3 units x 40.00 = 240.00, then 10.00 tax and 5.00 shipping
        assert order["subtotal"] == 240.0
        assert order["total_amount"] == 255.0
        assert order["item_count"] == 2
        assert order["total_units_ordered"] == 6
        assert order["total_units_received"] == 0

    async def test_line_totals_and_outstanding(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)

        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])

        line = order["items"][0]
        assert line["line_number"] == 1
        assert line["part_number"] == "BOS0986A"
        assert line["part_name"] == "Brake pad set, front"
        assert line["quantity_ordered"] == 10
        assert line["quantity_received"] == 0
        assert line["line_total"] == 400.0
        assert line["quantity_outstanding"] == 10
        assert line["is_fully_received"] is False

    async def test_expected_date_seeds_from_supplier_lead_time(
        self, parts_client: AsyncClient
    ):
        """A quoted lead time suggests a delivery date; it does not impose one."""
        supplier = await create_supplier(parts_client, lead_time_days=5)

        order = await create_order(parts_client, supplier["id"])

        expected = (date.today() + timedelta(days=5)).isoformat()
        assert order["expected_delivery_date"] == expected

    async def test_explicit_expected_date_wins(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client, lead_time_days=5)
        wanted = (date.today() + timedelta(days=10)).isoformat()

        order = await create_order(
            parts_client, supplier["id"], expected_delivery_date=wanted
        )

        assert order["expected_delivery_date"] == wanted

    async def test_zero_lead_time_leaves_the_date_open(
        self, parts_client: AsyncClient
    ):
        """A supplier with no quoted lead time gets no invented promise."""
        supplier = await create_supplier(parts_client, lead_time_days=0)

        order = await create_order(parts_client, supplier["id"])

        assert order["expected_delivery_date"] is None

    async def test_currency_is_normalised(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)

        order = await create_order(parts_client, supplier["id"], currency="eur")

        assert order["currency"] == "EUR"

    async def test_order_requires_at_least_one_line(self, parts_client: AsyncClient):
        """An order with nothing on it commits the shop to nothing."""
        supplier = await create_supplier(parts_client)

        response = await parts_client.post(
            ORDERS_URL, json={"supplier_id": supplier["id"], "items": []}
        )

        assert response.status_code == 422

    async def test_same_part_cannot_appear_twice(self, parts_client: AsyncClient):
        """Two lines for one part would make the receipt maths ambiguous."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)

        response = await parts_client.post(
            ORDERS_URL,
            json={
                "supplier_id": supplier["id"],
                "items": [
                    {"part_id": part["id"], "quantity_ordered": 5, "unit_cost": 40.0},
                    {"part_id": part["id"], "quantity_ordered": 7, "unit_cost": 40.0},
                ],
            },
        )

        assert response.status_code == 422
        assert "more than one line" in response.json()["detail"][0]["msg"]

    async def test_quantity_must_be_positive(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)

        response = await parts_client.post(
            ORDERS_URL,
            json={
                "supplier_id": supplier["id"],
                "items": [
                    {"part_id": part["id"], "quantity_ordered": 0, "unit_cost": 40.0}
                ],
            },
        )

        assert response.status_code == 422

    async def test_unknown_supplier_is_404(self, parts_client: AsyncClient):
        part = await create_part(parts_client)

        response = await parts_client.post(
            ORDERS_URL,
            json={
                "supplier_id": "2f0b4a1e-0000-4000-8000-000000000000",
                "items": [
                    {"part_id": part["id"], "quantity_ordered": 5, "unit_cost": 40.0}
                ],
            },
        )

        assert response.status_code == 404

    async def test_unknown_part_is_404(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)

        response = await parts_client.post(
            ORDERS_URL,
            json={
                "supplier_id": supplier["id"],
                "items": [
                    {
                        "part_id": "2f0b4a1e-0000-4000-8000-000000000000",
                        "quantity_ordered": 5,
                        "unit_cost": 40.0,
                    }
                ],
            },
        )

        assert response.status_code == 404

    async def test_discontinued_part_cannot_be_ordered(self, parts_client: AsyncClient):
        """Stock of a retired part cannot be received, so it must not be ordered."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        await parts_client.patch(
            f"/api/v1/parts/{part['id']}", json={"status": "DISCONTINUED"}
        )

        response = await parts_client.post(
            ORDERS_URL,
            json={
                "supplier_id": supplier["id"],
                "items": [
                    {"part_id": part["id"], "quantity_ordered": 5, "unit_cost": 40.0}
                ],
            },
        )

        assert response.status_code == 400
        assert "DISCONTINUED" in response.json()["detail"]

    async def test_retired_supplier_cannot_be_ordered(self, parts_client: AsyncClient):
        """A draft order against a retired supplier could never be delivered."""
        supplier = await create_supplier(parts_client)
        await parts_client.post(f"{SUPPLIERS_URL}{supplier['id']}/deactivate")

        response = await parts_client.post(
            ORDERS_URL,
            json={
                "supplier_id": supplier["id"],
                "items": [
                    {
                        "part_id": (await create_part(parts_client))["id"],
                        "quantity_ordered": 5,
                        "unit_cost": 40.0,
                    }
                ],
            },
        )

        assert response.status_code == 400
        assert "INACTIVE" in response.json()["detail"]

    async def test_expected_date_cannot_precede_the_order(
        self, parts_client: AsyncClient
    ):
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()

        response = await parts_client.post(
            ORDERS_URL,
            json={
                "supplier_id": supplier["id"],
                "order_date": tomorrow,
                "expected_delivery_date": yesterday,
                "items": [
                    {"part_id": part["id"], "quantity_ordered": 5, "unit_cost": 40.0}
                ],
            },
        )

        assert response.status_code == 422

    async def test_negative_tax_is_rejected(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)

        response = await parts_client.post(
            ORDERS_URL,
            json={
                "supplier_id": supplier["id"],
                "tax_amount": -5,
                "items": [
                    {"part_id": part["id"], "quantity_ordered": 5, "unit_cost": 40.0}
                ],
            },
        )

        assert response.status_code == 422

    async def test_lookup_by_number(self, parts_client: AsyncClient):
        """A delivery note carries the number, not the id."""
        supplier = await create_supplier(parts_client)
        order = await create_order(parts_client, supplier["id"])

        response = await parts_client.get(f"{ORDERS_URL}by-number/{order['po_number']}")

        assert response.status_code == 200
        assert response.json()["id"] == order["id"]

    async def test_list_filters(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client, name="Delivery Co")
        other = await create_supplier(parts_client, name="Tyre Depot")
        order = await create_order(parts_client, supplier["id"])
        await create_order(parts_client, other["id"])

        all_orders = await parts_client.get(ORDERS_URL)
        drafts = await parts_client.get(ORDERS_URL, params={"status": "DRAFT"})
        by_supplier = await parts_client.get(
            ORDERS_URL, params={"supplier_id": supplier["id"]}
        )
        sent = await send_order(parts_client, order["id"])
        by_status = await parts_client.get(ORDERS_URL, params={"status": "SENT"})

        assert all_orders.json()["meta"]["total"] == 2
        assert drafts.json()["meta"]["total"] == 2
        assert by_supplier.json()["meta"]["total"] == 1
        assert by_supplier.json()["data"][0]["id"] == order["id"]
        assert by_status.json()["meta"]["total"] == 1
        assert by_status.json()["data"][0]["id"] == sent["id"]

    async def test_overdue_filter(self, parts_client: AsyncClient):
        """A past delivery date on an open order is the thing to chase."""
        supplier = await create_supplier(parts_client)
        late = await create_order(
            parts_client,
            supplier["id"],
            expected_delivery_date=(date.today() - timedelta(days=2)).isoformat(),
        )
        await create_order(parts_client, supplier["id"])

        response = await parts_client.get(ORDERS_URL, params={"overdue_only": True})

        data = response.json()["data"]
        assert response.json()["meta"]["total"] == 1
        assert data[0]["id"] == late["id"]
        assert data[0]["is_overdue"] is True


class TestPurchaseOrderDraftEdits:
    """Editing an order while it is still a draft."""

    async def test_add_line(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)
        order = await create_order(parts_client, supplier["id"])
        extra = await create_part(parts_client, part_number="P-2", sku="S-2", name="Two")

        response = await parts_client.post(
            f"{ORDERS_URL}{order['id']}/items",
            json={"part_id": extra["id"], "quantity_ordered": 4, "unit_cost": 12.5},
        )

        body = response.json()
        assert response.status_code == 201
        assert body["item_count"] == 2
        assert body["items"][1]["line_number"] == 2
        assert body["items"][1]["part_number"] == "P-2"
        # 10 x 40.00 + 4 x 12.50
        assert body["subtotal"] == 450.0

    async def test_add_duplicate_part_refused(self, parts_client: AsyncClient):
        """A second line for the same part makes receipt maths ambiguous."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])

        response = await parts_client.post(
            f"{ORDERS_URL}{order['id']}/items",
            json={"part_id": part["id"], "quantity_ordered": 4, "unit_cost": 12.5},
        )

        assert response.status_code == 409
        assert "already on" in response.json()["detail"]

    async def test_update_line(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)
        order = await create_order(parts_client, supplier["id"])
        line_id = order["items"][0]["id"]

        response = await parts_client.patch(
            f"{ORDERS_URL}{order['id']}/items/{line_id}",
            json={"quantity_ordered": 20, "unit_cost": 35.0, "notes": "genuine part"},
        )

        assert response.status_code == 200
        assert response.json()["quantity_ordered"] == 20
        assert response.json()["line_total"] == 700.0

        refreshed = await parts_client.get(f"{ORDERS_URL}{order['id']}")
        assert refreshed.json()["subtotal"] == 700.0

    async def test_remove_line_renumbers_the_rest(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)
        first = await create_part(parts_client, part_number="P-1", sku="S-1", name="One")
        second = await create_part(parts_client, part_number="P-2", sku="S-2", name="Two")
        third = await create_part(parts_client, part_number="P-3", sku="S-3", name="Three")
        order = await create_order(
            parts_client, supplier["id"], part_ids=[first["id"], second["id"], third["id"]]
        )

        response = await parts_client.delete(
            f"{ORDERS_URL}{order['id']}/items/{order['items'][1]['id']}"
        )

        body = response.json()
        assert response.status_code == 200
        assert [line["line_number"] for line in body["items"]] == [1, 2]
        assert [line["part_number"] for line in body["items"]] == ["P-1", "P-3"]
        assert body["item_count"] == 2

    async def test_last_line_cannot_be_removed(self, parts_client: AsyncClient):
        """An order with nothing on it should be cancelled or deleted instead."""
        supplier = await create_supplier(parts_client)
        order = await create_order(parts_client, supplier["id"])

        response = await parts_client.delete(
            f"{ORDERS_URL}{order['id']}/items/{order['items'][0]['id']}"
        )

        assert response.status_code == 400
        assert "last line" in response.json()["detail"]

    async def test_patch_replaces_every_line(self, parts_client: AsyncClient):
        """One request edits the whole order, rather than juggling the collection."""
        supplier = await create_supplier(parts_client)
        original = await create_part(parts_client)
        replacement = await create_part(
            parts_client, part_number="P-9", sku="S-9", name="Replacement"
        )
        order = await create_order(parts_client, supplier["id"], part_ids=[original["id"]])

        response = await parts_client.patch(
            f"{ORDERS_URL}{order['id']}",
            json={
                "shipping_amount": 12.0,
                "items": [
                    {"part_id": replacement["id"], "quantity_ordered": 6, "unit_cost": 20.0}
                ],
            },
        )

        body = response.json()
        assert response.status_code == 200
        assert body["item_count"] == 1
        assert body["items"][0]["part_number"] == "P-9"
        assert body["subtotal"] == 120.0
        assert body["total_amount"] == 132.0

    async def test_sent_order_is_frozen(self, parts_client: AsyncClient):
        """The supplier may already hold the goods, so the lines stop moving."""
        supplier = await create_supplier(parts_client)
        order = await create_order(parts_client, supplier["id"])
        await send_order(parts_client, order["id"])

        response = await parts_client.patch(
            f"{ORDERS_URL}{order['id']}",
            json={"notes": "changed my mind"},
        )

        assert response.status_code == 400
        assert "only a DRAFT" in response.json()["detail"]

    async def test_sent_order_rejects_new_lines(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)
        order = await create_order(parts_client, supplier["id"])
        await send_order(parts_client, order["id"])
        extra = await create_part(parts_client, part_number="P-2", sku="S-2", name="Two")

        response = await parts_client.post(
            f"{ORDERS_URL}{order['id']}/items",
            json={"part_id": extra["id"], "quantity_ordered": 4, "unit_cost": 12.5},
        )

        assert response.status_code == 400

    async def test_delete_draft(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)
        order = await create_order(parts_client, supplier["id"])

        response = await parts_client.delete(f"{ORDERS_URL}{order['id']}")

        assert response.status_code == 204
        assert (await parts_client.get(f"{ORDERS_URL}{order['id']}")).status_code == 404

    async def test_sent_order_cannot_be_deleted(self, parts_client: AsyncClient):
        """A sent order is a commitment; it is cancelled, not erased."""
        supplier = await create_supplier(parts_client)
        order = await create_order(parts_client, supplier["id"])
        await send_order(parts_client, order["id"])

        response = await parts_client.delete(f"{ORDERS_URL}{order['id']}")

        assert response.status_code == 400
        assert "cancel it instead" in response.json()["detail"]


class TestPurchaseOrderStatusMachine:
    """How an order moves through its lifecycle."""

    async def test_send_stamps_the_milestone(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)
        order = await create_order(parts_client, supplier["id"])

        response = await parts_client.post(f"{ORDERS_URL}{order['id']}/send")

        body = response.json()
        assert response.status_code == 200
        assert body["status"] == "SENT"
        assert body["sent_at"] is not None
        assert body["is_editable"] is False

    async def test_send_twice_is_harmless(self, parts_client: AsyncClient):
        """Re-sending is a no-op rather than an error: a phone call repeats."""
        supplier = await create_supplier(parts_client)
        order = await create_order(parts_client, supplier["id"])
        await send_order(parts_client, order["id"])

        response = await parts_client.post(f"{ORDERS_URL}{order['id']}/send")

        assert response.status_code == 200
        assert response.json()["status"] == "SENT"

    async def test_send_refused_after_cancel(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)
        order = await create_order(parts_client, supplier["id"])
        await parts_client.post(f"{ORDERS_URL}{order['id']}/cancel")

        response = await parts_client.post(f"{ORDERS_URL}{order['id']}/send")

        assert response.status_code == 400
        assert "terminal state" in response.json()["detail"]

    async def test_cancel_records_the_reason(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)
        order = await create_order(parts_client, supplier["id"])

        response = await parts_client.post(
            f"{ORDERS_URL}{order['id']}/cancel",
            json={"reason": "Supplier out of stock until March"},
        )

        body = response.json()
        assert response.status_code == 200
        assert body["status"] == "CANCELLED"
        assert body["cancel_reason"] == "Supplier out of stock until March"
        assert body["cancelled_at"] is not None
        assert body["is_terminal"] is True

    async def test_cancel_without_a_body(self, parts_client: AsyncClient):
        """The reason is optional, so the request need not carry one."""
        supplier = await create_supplier(parts_client)
        order = await create_order(parts_client, supplier["id"])

        response = await parts_client.post(f"{ORDERS_URL}{order['id']}/cancel")

        assert response.status_code == 200
        assert response.json()["cancel_reason"] is None

    async def test_received_cannot_be_cancelled(self, parts_client: AsyncClient):
        """Goods that arrived and were paid for are not undone by paperwork."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])
        order = await send_order(parts_client, order["id"])
        await receive(parts_client, order["id"], [{"item_id": order["items"][0]["id"], "quantity": 10}])

        response = await parts_client.post(f"{ORDERS_URL}{order['id']}/cancel")

        assert response.status_code == 400

    async def test_status_endpoint_moves_a_draft(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)
        order = await create_order(parts_client, supplier["id"])

        response = await parts_client.patch(
            f"{ORDERS_URL}{order['id']}/status", json={"status": "SENT"}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "SENT"
        assert response.json()["sent_at"] is not None

    async def test_status_endpoint_routes_through_send_guards(
        self, parts_client: AsyncClient
    ):
        """A retired supplier blocks the order however the transition is asked for."""
        supplier = await create_supplier(parts_client)
        order = await create_order(parts_client, supplier["id"])
        await parts_client.patch(
            f"{SUPPLIERS_URL}{supplier['id']}", json={"status": "INACTIVE"}
        )

        response = await parts_client.patch(
            f"{ORDERS_URL}{order['id']}/status", json={"status": "SENT"}
        )

        assert response.status_code == 400
        assert "reactivate" in response.json()["detail"]

    async def test_received_cannot_be_set_directly(self, parts_client: AsyncClient):
        """An order may not claim a delivery that was never booked in."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])
        await send_order(parts_client, order["id"])

        response = await parts_client.patch(
            f"{ORDERS_URL}{order['id']}/status", json={"status": "RECEIVED"}
        )

        assert response.status_code == 400
        assert "receiving a delivery" in response.json()["detail"]

        # The order is untouched, and nothing has appeared on the shelf.
        refreshed = (await parts_client.get(f"{ORDERS_URL}{order['id']}")).json()
        assert refreshed["status"] == "SENT"
        stock = (await parts_client.get(f"/api/v1/parts/{part['id']}")).json()
        assert stock["quantity_on_hand"] == 0

    async def test_unknown_status_is_rejected(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)
        order = await create_order(parts_client, supplier["id"])

        response = await parts_client.patch(
            f"{ORDERS_URL}{order['id']}/status", json={"status": "DELIVERED"}
        )

        assert response.status_code == 422

    async def test_summary_counts_the_order_book(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)
        draft = await create_order(parts_client, supplier["id"])
        sent = await create_order(parts_client, supplier["id"])
        await send_order(parts_client, sent["id"])
        cancelled = await create_order(parts_client, supplier["id"])
        await parts_client.post(f"{ORDERS_URL}{cancelled['id']}/cancel")

        body = (await parts_client.get(f"{ORDERS_URL}summary")).json()

        assert body["total_orders"] == 3
        assert body["draft_orders"] == 1
        # Drafts are still open orders: the shop is on the hook for them.
        assert body["open_orders"] == 2
        assert body["cancelled_orders"] == 1
        assert body["total_committed"] > 0
        assert draft["status"] == "DRAFT"


# --- receiving --------------------------------------------------------------


class TestReceivingWorkflow:
    """Booking a delivery in, and the stock it must produce."""

    async def test_receiving_updates_inventory(self, parts_client: AsyncClient):
        """Goods from a supplier land on the same ledger as everything else."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])
        order = await send_order(parts_client, order["id"])

        result = await receive(
            parts_client, order["id"], [{"item_id": order["items"][0]["id"], "quantity": 10}]
        )

        assert result["received_units"] == 10
        assert len(result["receipts"]) == 1
        receipt = result["receipts"][0]
        assert receipt["part_number"] == "BOS0986A"
        assert receipt["quantity"] == 10
        assert receipt["unit_cost"] == 40.0

        stock = (await parts_client.get(f"/api/v1/parts/{part['id']}")).json()
        assert stock["quantity_on_hand"] == 10

    async def test_receipt_is_filed_against_the_order_number(
        self, parts_client: AsyncClient
    ):
        """The ledger points back at the order that caused the movement."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])
        order = await send_order(parts_client, order["id"])
        await receive(
            parts_client, order["id"], [{"item_id": order["items"][0]["id"], "quantity": 4}]
        )

        history = (
            await parts_client.get(f"/api/v1/inventory/parts/{part['id']}/history")
        ).json()

        assert len(history) == 1
        assert history[0]["transaction_type"] == "RECEIPT"
        assert history[0]["reference"] == order["po_number"]
        assert history[0]["quantity"] == 4
        assert history[0]["quantity_before"] == 0
        assert history[0]["quantity_after"] == 4

    async def test_full_receipt_closes_the_order(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])
        order = await send_order(parts_client, order["id"])

        result = await receive(
            parts_client, order["id"], [{"item_id": order["items"][0]["id"], "quantity": 10}]
        )

        po = result["purchase_order"]
        assert po["status"] == "RECEIVED"
        assert po["received_at"] is not None
        assert po["is_fully_received"] is True
        assert po["is_terminal"] is True
        assert po["total_units_received"] == 10
        assert po["items"][0]["is_fully_received"] is True
        assert po["items"][0]["quantity_outstanding"] == 0

    async def test_partial_receipt_keeps_the_order_open(
        self, parts_client: AsyncClient
    ):
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])
        order = await send_order(parts_client, order["id"])

        result = await receive(
            parts_client, order["id"], [{"item_id": order["items"][0]["id"], "quantity": 4}]
        )

        po = result["purchase_order"]
        assert po["status"] == "PARTIALLY_RECEIVED"
        assert po["received_at"] is None
        assert po["is_fully_received"] is False
        assert po["items"][0]["quantity_outstanding"] == 6

    async def test_receipts_accumulate_across_deliveries(
        self, parts_client: AsyncClient
    ):
        """Suppliers deliver in instalments; the counts add up."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])
        order = await send_order(parts_client, order["id"])
        line_id = order["items"][0]["id"]

        await receive(parts_client, order["id"], [{"item_id": line_id, "quantity": 4}])
        result = await receive(
            parts_client, order["id"], [{"item_id": line_id, "quantity": 6}]
        )

        assert result["purchase_order"]["status"] == "RECEIVED"
        assert result["purchase_order"]["items"][0]["quantity_received"] == 10

        stock = (await parts_client.get(f"/api/v1/parts/{part['id']}")).json()
        assert stock["quantity_on_hand"] == 10

        history = (
            await parts_client.get(f"/api/v1/inventory/parts/{part['id']}/history")
        ).json()
        assert len(history) == 2
        # The ledger is append-only: each delivery is its own row.
        assert sorted(row["quantity_after"] for row in history) == [4, 10]

    async def test_multi_line_order_needs_every_line(
        self, parts_client: AsyncClient
    ):
        """A part delivered in full does not close an order with another line out."""
        supplier = await create_supplier(parts_client)
        first = await create_part(parts_client, part_number="P-1", sku="S-1", name="One")
        second = await create_part(parts_client, part_number="P-2", sku="S-2", name="Two")
        order = await create_order(
            parts_client, supplier["id"], part_ids=[first["id"], second["id"]]
        )
        order = await send_order(parts_client, order["id"])
        first_line, second_line = order["items"]

        result = await receive(
            parts_client, order["id"], [{"item_id": first_line["id"], "quantity": 10}]
        )

        assert result["purchase_order"]["status"] == "PARTIALLY_RECEIVED"
        assert result["purchase_order"]["is_fully_received"] is False

        result = await receive(
            parts_client, order["id"], [{"item_id": second_line["id"], "quantity": 10}]
        )
        assert result["purchase_order"]["status"] == "RECEIVED"

    async def test_partial_line_keeps_the_order_open(self, parts_client: AsyncClient):
        """Half a line is not a closed line."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])
        order = await send_order(parts_client, order["id"])

        result = await receive(
            parts_client, order["id"], [{"item_id": order["items"][0]["id"], "quantity": 9}]
        )

        assert result["purchase_order"]["status"] == "PARTIALLY_RECEIVED"
        assert result["purchase_order"]["is_fully_received"] is False

    async def test_receiving_uses_the_invoice_price(
        self, parts_client: AsyncClient
    ):
        """What the supplier actually charged is captured on the receipt."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])
        order = await send_order(parts_client, order["id"])

        result = await receive(
            parts_client,
            order["id"],
            [{"item_id": order["items"][0]["id"], "quantity": 5, "unit_cost": 38.75}],
        )

        assert result["receipts"][0]["unit_cost"] == 38.75

        history = (
            await parts_client.get(f"/api/v1/inventory/parts/{part['id']}/history")
        ).json()
        assert history[0]["unit_cost"] == 38.75
        assert history[0]["quantity"] == 5

    async def test_invoice_price_does_not_reprice_the_catalog(
        self, parts_client: AsyncClient
    ):
        """A one-off delivery price is history; the catalog keeps its own figure."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])
        order = await send_order(parts_client, order["id"])

        await receive(
            parts_client,
            order["id"],
            [{"item_id": order["items"][0]["id"], "quantity": 5, "unit_cost": 10.0}],
        )

        stock = (await parts_client.get(f"/api/v1/parts/{part['id']}")).json()
        assert stock["unit_cost"] == 42.50

        # The order keeps what was agreed; the receipt keeps what was paid.
        refreshed = (await parts_client.get(f"{ORDERS_URL}{order['id']}")).json()
        assert refreshed["items"][0]["unit_cost"] == 40.0

    async def test_over_receipt_is_refused(self, parts_client: AsyncClient):
        """More than was ordered is agreed as a new line, not absorbed silently."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])
        order = await send_order(parts_client, order["id"])

        response = await parts_client.post(
            f"{ORDERS_URL}{order['id']}/receive",
            json={"items": [{"item_id": order["items"][0]["id"], "quantity": 11}]},
        )

        assert response.status_code == 400
        assert "outstanding" in response.json()["detail"]

    async def test_over_receipt_after_a_partial_delivery_is_refused(
        self, parts_client: AsyncClient
    ):
        """The limit is the outstanding quantity, not the original order."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])
        order = await send_order(parts_client, order["id"])
        line_id = order["items"][0]["id"]
        await receive(parts_client, order["id"], [{"item_id": line_id, "quantity": 8}])

        response = await parts_client.post(
            f"{ORDERS_URL}{order['id']}/receive",
            json={"items": [{"item_id": line_id, "quantity": 3}]},
        )

        assert response.status_code == 400
        stock = (await parts_client.get(f"/api/v1/parts/{part['id']}")).json()
        assert stock["quantity_on_hand"] == 8

    async def test_fully_received_line_takes_no_more(self, parts_client: AsyncClient):
        """A line that is already complete cannot be topped up.

        The order still has a second line outstanding, so the request reaches the
        line itself rather than being refused as a closed order.
        """
        supplier = await create_supplier(parts_client)
        first = await create_part(parts_client, part_number="P-1", sku="S-1", name="One")
        second = await create_part(parts_client, part_number="P-2", sku="S-2", name="Two")
        order = await create_order(
            parts_client, supplier["id"], part_ids=[first["id"], second["id"]]
        )
        order = await send_order(parts_client, order["id"])
        first_line, second_line = order["items"]
        await receive(parts_client, order["id"], [{"item_id": first_line["id"], "quantity": 10}])

        response = await parts_client.post(
            f"{ORDERS_URL}{order['id']}/receive",
            json={"items": [{"item_id": first_line["id"], "quantity": 1}]},
        )

        assert response.status_code == 400
        assert "already been fully received" in response.json()["detail"]
        stock = (await parts_client.get(f"/api/v1/parts/{first['id']}")).json()
        assert stock["quantity_on_hand"] == 10
        assert second_line["id"] != first_line["id"]

    async def test_a_refused_line_moves_no_stock_at_all(
        self, parts_client: AsyncClient
    ):
        """One delivery, one transaction: a bad line must not half-book the good ones."""
        supplier = await create_supplier(parts_client)
        first = await create_part(parts_client, part_number="P-1", sku="S-1", name="One")
        second = await create_part(parts_client, part_number="P-2", sku="S-2", name="Two")
        order = await create_order(
            parts_client, supplier["id"], part_ids=[first["id"], second["id"]]
        )
        order = await send_order(parts_client, order["id"])
        first_line, second_line = order["items"]

        response = await parts_client.post(
            f"{ORDERS_URL}{order['id']}/receive",
            json={
                "items": [
                    {"item_id": first_line["id"], "quantity": 10},
                    {"item_id": second_line["id"], "quantity": 99},
                ]
            },
        )

        assert response.status_code == 400

        # Neither line moved, and the order is still open and unshipped.
        first_stock = (await parts_client.get(f"/api/v1/parts/{first['id']}")).json()
        second_stock = (await parts_client.get(f"/api/v1/parts/{second['id']}")).json()
        assert first_stock["quantity_on_hand"] == 0
        assert second_stock["quantity_on_hand"] == 0

        refreshed = (await parts_client.get(f"{ORDERS_URL}{order['id']}")).json()
        assert refreshed["status"] == "SENT"
        assert refreshed["items"][0]["quantity_received"] == 0

        ledger = (
            await parts_client.get("/api/v1/inventory/transactions")
        ).json()
        assert ledger["meta"]["total"] == 0

    async def test_receiving_against_a_draft_is_refused(
        self, parts_client: AsyncClient
    ):
        """Goods cannot arrive for an order the supplier has not been given."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])

        response = await parts_client.post(
            f"{ORDERS_URL}{order['id']}/receive",
            json={"items": [{"item_id": order["items"][0]["id"], "quantity": 10}]},
        )

        assert response.status_code == 400
        assert "Send it first" in response.json()["detail"]

    async def test_receiving_a_cancelled_order_is_refused(
        self, parts_client: AsyncClient
    ):
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])
        order = await send_order(parts_client, order["id"])
        await parts_client.post(f"{ORDERS_URL}{order['id']}/cancel")

        response = await parts_client.post(
            f"{ORDERS_URL}{order['id']}/receive",
            json={"items": [{"item_id": order["items"][0]["id"], "quantity": 10}]},
        )

        assert response.status_code == 400

    async def test_unknown_line_is_404(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)
        order = await create_order(parts_client, supplier["id"])
        order = await send_order(parts_client, order["id"])

        response = await parts_client.post(
            f"{ORDERS_URL}{order['id']}/receive",
            json={"items": [{"item_id": "2f0b4a1e-0000-4000-8000-000000000000", "quantity": 1}]},
        )

        assert response.status_code == 404

    async def test_same_line_cannot_be_listed_twice(self, parts_client: AsyncClient):
        """A duplicated line would leave the ordered quantity ambiguous."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])
        order = await send_order(parts_client, order["id"])
        line_id = order["items"][0]["id"]

        response = await parts_client.post(
            f"{ORDERS_URL}{order['id']}/receive",
            json={"items": [{"item_id": line_id, "quantity": 2}, {"item_id": line_id, "quantity": 3}]},
        )

        assert response.status_code == 422
        assert "twice" in response.json()["detail"][0]["msg"]

    async def test_receipt_requires_a_positive_quantity(
        self, parts_client: AsyncClient
    ):
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])
        order = await send_order(parts_client, order["id"])

        response = await parts_client.post(
            f"{ORDERS_URL}{order['id']}/receive",
            json={"items": [{"item_id": order["items"][0]["id"], "quantity": 0}]},
        )

        assert response.status_code == 422

    async def test_receiving_an_empty_delivery_is_rejected(
        self, parts_client: AsyncClient
    ):
        supplier = await create_supplier(parts_client)
        order = await create_order(parts_client, supplier["id"])
        order = await send_order(parts_client, order["id"])

        response = await parts_client.post(f"{ORDERS_URL}{order['id']}/receive", json={"items": []})

        assert response.status_code == 422

    async def test_receiving_a_retired_suppliers_delivery_is_allowed(
        self, parts_client: AsyncClient
    ):
        """Goods on the doorstep are booked in even from a supplier now retired."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])
        order = await send_order(parts_client, order["id"])
        await parts_client.patch(f"{SUPPLIERS_URL}{supplier['id']}", json={"status": "INACTIVE"})

        result = await receive(
            parts_client, order["id"], [{"item_id": order["items"][0]["id"], "quantity": 10}]
        )

        assert result["purchase_order"]["status"] == "RECEIVED"

    async def test_receipt_note_is_kept_on_the_order(
        self, parts_client: AsyncClient
    ):
        """A note about the delivery stays with the order it belongs to."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])
        order = await send_order(parts_client, order["id"])

        response = await parts_client.post(
            f"{ORDERS_URL}{order['id']}/receive",
            json={
                "items": [{"item_id": order["items"][0]["id"], "quantity": 10}],
                "notes": "Two boxes, one short-dated",
            },
        )

        assert response.status_code == 200
        assert "Two boxes" in response.json()["purchase_order"]["internal_notes"]

    async def test_receiving_keeps_the_ledger_reconcilable(
        self, parts_client: AsyncClient, db: AsyncSession
    ):
        """The balance and its ledger must agree after a delivery."""
        supplier = await create_supplier(parts_client)
        first = await create_part(parts_client, part_number="P-1", sku="S-1", name="One")
        second = await create_part(parts_client, part_number="P-2", sku="S-2", name="Two")
        order = await create_order(
            parts_client, supplier["id"], part_ids=[first["id"], second["id"]]
        )
        order = await send_order(parts_client, order["id"])
        first_line, second_line = order["items"]
        await receive(parts_client, order["id"], [{"item_id": first_line["id"], "quantity": 3}])
        await receive(
            parts_client,
            order["id"],
            [
                {"item_id": first_line["id"], "quantity": 2},
                {"item_id": second_line["id"], "quantity": 7},
            ],
        )

        for part_id in (first["id"], second["id"]):
            part = (
                await db.execute(select(Part).where(Part.id == part_id))
            ).scalar_one()
            ledger = (
                await db.execute(
                    select(func.coalesce(func.sum(InventoryTransaction.quantity), 0.0)).where(
                        InventoryTransaction.part_id == part_id
                    )
                )
            ).scalar_one()
            assert round(float(part.quantity_on_hand), 2) == round(float(ledger), 2)

        receipts = (
            await db.execute(
                select(InventoryTransaction).where(
                    InventoryTransaction.transaction_type
                    == InventoryTransactionType.RECEIPT.value
                )
            )
        ).scalars().all()
        assert len(receipts) == 3
        assert all(row.reference == order["po_number"] for row in receipts)

    async def test_receiving_clears_the_low_stock_flag(
        self, parts_client: AsyncClient
    ):
        """The reorder alert follows the balance, so buying drops the part off it."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)  # reorder_level 4, on hand 0
        before = (await parts_client.get("/api/v1/parts/low-stock")).json()
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])
        order = await send_order(parts_client, order["id"])
        await receive(parts_client, order["id"], [{"item_id": order["items"][0]["id"], "quantity": 10}])

        after = (await parts_client.get("/api/v1/parts/low-stock")).json()

        assert any(row["part_id"] == part["id"] for row in before)
        assert all(row["part_id"] != part["id"] for row in after)

    async def test_cancelling_a_partly_received_order_keeps_the_stock(
        self, parts_client: AsyncClient
    ):
        """Cancelling the paperwork does not un-buy goods already on the shelf."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])
        order = await send_order(parts_client, order["id"])
        await receive(
            parts_client, order["id"], [{"item_id": order["items"][0]["id"], "quantity": 4}]
        )

        response = await parts_client.post(
            f"{ORDERS_URL}{order['id']}/cancel", json={"reason": "Rest of the order dropped"}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "CANCELLED"
        stock = (await parts_client.get(f"/api/v1/parts/{part['id']}")).json()
        assert stock["quantity_on_hand"] == 4


# --- reorder from low stock --------------------------------------------------


class TestReorderFromLowStock:
    """Raising a draft order straight from the buying list."""

    async def test_order_covers_every_low_stock_part(self, parts_client: AsyncClient):
        """Every part on the buying list is covered; healthy ones are left out."""
        supplier = await create_supplier(parts_client)
        low = await create_part(parts_client, part_number="LOW-1", sku="S-1", name="Low one")
        stocked = await create_part(
            parts_client, part_number="OK-2", sku="S-3", name="Above reorder level"
        )
        await parts_client.post(
            "/api/v1/inventory/transactions",
            json={"part_id": stocked["id"], "transaction_type": "RECEIPT", "quantity": 50},
        )

        response = await parts_client.post(
            f"{ORDERS_URL}from-low-stock", json={"supplier_id": supplier["id"]}
        )

        body = response.json()
        assert response.status_code == 201
        assert [line["part_number"] for line in body["items"]] == ["LOW-1"]
        assert body["status"] == "DRAFT"
        assert low["id"] == body["items"][0]["part_id"]

    async def test_quantity_follows_the_shortage(self, parts_client: AsyncClient):
        """Two units short of a reorder level of four, doubled, is four units."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client, reorder_level=4)
        await parts_client.post(
            "/api/v1/inventory/transactions",
            json={"part_id": part["id"], "transaction_type": "RECEIPT", "quantity": 2},
        )

        response = await parts_client.post(
            f"{ORDERS_URL}from-low-stock",
            json={"supplier_id": supplier["id"], "shortage_multiplier": 2},
        )

        assert response.json()["items"][0]["quantity_ordered"] == 4

    async def test_multiplier_of_one_buys_exactly_the_shortage(
        self, parts_client: AsyncClient
    ):
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client, reorder_level=4)
        await parts_client.post(
            "/api/v1/inventory/transactions",
            json={"part_id": part["id"], "transaction_type": "RECEIPT", "quantity": 1},
        )

        response = await parts_client.post(
            f"{ORDERS_URL}from-low-stock",
            json={"supplier_id": supplier["id"], "shortage_multiplier": 1},
        )

        assert response.json()["items"][0]["quantity_ordered"] == 3

    async def test_never_orders_zero_units(self, parts_client: AsyncClient):
        """A part sitting exactly on its reorder point is still due to be bought."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client, reorder_level=3)
        await parts_client.post(
            "/api/v1/inventory/transactions",
            json={"part_id": part["id"], "transaction_type": "RECEIPT", "quantity": 3},
        )

        response = await parts_client.post(
            f"{ORDERS_URL}from-low-stock",
            json={"supplier_id": supplier["id"], "shortage_multiplier": 1},
        )

        assert response.json()["items"][0]["quantity_ordered"] == 1

    async def test_healthy_parts_are_left_out(self, parts_client: AsyncClient):
        """A part above its reorder level is not something to reorder."""
        supplier = await create_supplier(parts_client)
        stocked = await create_part(parts_client, part_number="OK-1", sku="S-1", name="Healthy")
        await parts_client.post(
            "/api/v1/inventory/transactions",
            json={"part_id": stocked["id"], "transaction_type": "RECEIPT", "quantity": 40},
        )
        await create_part(parts_client, part_number="LOW-1", sku="S-2", name="Low")

        response = await parts_client.post(
            f"{ORDERS_URL}from-low-stock", json={"supplier_id": supplier["id"]}
        )

        assert [line["part_number"] for line in response.json()["items"]] == ["LOW-1"]

    async def test_discontinued_parts_are_excluded_by_default(
        self, parts_client: AsyncClient
    ):
        """A part the shop no longer trades in is not worth reordering."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        await parts_client.patch(
            f"/api/v1/parts/{part['id']}", json={"status": "DISCONTINUED"}
        )

        refused = await parts_client.post(
            f"{ORDERS_URL}from-low-stock", json={"supplier_id": supplier["id"]}
        )
        included = await parts_client.post(
            f"{ORDERS_URL}from-low-stock",
            json={"supplier_id": supplier["id"], "include_discontinued": True},
        )

        assert refused.status_code == 400
        assert included.status_code == 201
        assert [line["part_number"] for line in included.json()["items"]] == ["BOS0986A"]

    async def test_nothing_low_stock_is_refused(self, parts_client: AsyncClient):
        """An empty buying list must not produce an empty order."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        await parts_client.post(
            "/api/v1/inventory/transactions",
            json={"part_id": part["id"], "transaction_type": "RECEIPT", "quantity": 40},
        )

        response = await parts_client.post(
            f"{ORDERS_URL}from-low-stock", json={"supplier_id": supplier["id"]}
        )

        assert response.status_code == 400
        assert "nothing to order" in response.json()["detail"]

    async def test_part_filter_narrows_the_order(self, parts_client: AsyncClient):
        supplier = await create_supplier(parts_client)
        wanted = await create_part(parts_client, part_number="WANT-1", sku="S-1", name="Wanted")
        await create_part(parts_client, part_number="LOW-2", sku="S-2", name="Also low")

        response = await parts_client.post(
            f"{ORDERS_URL}from-low-stock",
            json={"supplier_id": supplier["id"], "part_ids": [wanted["id"]]},
        )

        assert [line["part_number"] for line in response.json()["items"]] == ["WANT-1"]

    async def test_reorder_uses_the_catalog_cost_as_a_starting_price(
        self, parts_client: AsyncClient
    ):
        """The catalog cost seeds the line; the invoice price wins at receipt."""
        supplier = await create_supplier(parts_client)
        await create_part(parts_client, unit_cost=42.50)

        response = await parts_client.post(
            f"{ORDERS_URL}from-low-stock", json={"supplier_id": supplier["id"]}
        )

        assert response.json()["items"][0]["unit_cost"] == 42.50

    async def test_reorder_order_can_be_sent_and_received(
        self, parts_client: AsyncClient
    ):
        """The generated order is a normal draft: it goes through the same workflow."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        await parts_client.post(
            "/api/v1/inventory/transactions",
            json={"part_id": part["id"], "transaction_type": "RECEIPT", "quantity": 2},
        )
        created = (
            await parts_client.post(
                f"{ORDERS_URL}from-low-stock", json={"supplier_id": supplier["id"]}
            )
        ).json()

        sent = await send_order(parts_client, created["id"])
        result = await receive(
            parts_client,
            sent["id"],
            [{"item_id": sent["items"][0]["id"], "quantity": sent["items"][0]["quantity_ordered"]}],
        )

        assert result["purchase_order"]["status"] == "RECEIVED"
        stock = (await parts_client.get(f"/api/v1/parts/{part['id']}")).json()
        assert stock["quantity_on_hand"] == 6

    async def test_reorder_against_a_retired_supplier_is_refused(
        self, parts_client: AsyncClient
    ):
        supplier = await create_supplier(parts_client)
        await parts_client.post(f"{SUPPLIERS_URL}{supplier['id']}/deactivate")
        await create_part(parts_client)

        response = await parts_client.post(
            f"{ORDERS_URL}from-low-stock", json={"supplier_id": supplier["id"]}
        )

        assert response.status_code == 400
        assert "INACTIVE" in response.json()["detail"]


# --- access control ---------------------------------------------------------


class TestPurchaseOrderAccess:
    """Who may see and place orders."""

    async def test_parts_staff_manage_suppliers_and_orders(self, parts_client: AsyncClient):
        """The parts department runs the buying side of the shop."""
        supplier = await create_supplier(parts_client)
        order = await create_order(parts_client, supplier["id"])

        assert (await parts_client.get(SUPPLIERS_URL)).status_code == 200
        assert (await parts_client.get(ORDERS_URL)).status_code == 200
        assert (await parts_client.get(f"{ORDERS_URL}summary")).status_code == 200
        assert (
            await parts_client.get(f"{SUPPLIERS_URL}{supplier['id']}/summary")
        ).status_code == 200
        assert order["status"] == "DRAFT"

    async def test_parts_staff_can_receive(self, parts_client: AsyncClient):
        """The department that may move stock holds the purchase order."""
        order = await sent_order(parts_client)

        result = await receive(
            parts_client, order["id"], [{"item_id": order["items"][0]["id"], "quantity": 10}]
        )

        assert result["purchase_order"]["status"] == "RECEIVED"

    async def test_owner_can_manage_orders(self, owner_client: AsyncClient):
        """The owner inherits every permission, including the buying ones."""
        supplier = await create_supplier(owner_client, name="Owner Supplier")

        response = await owner_client.post(
            ORDERS_URL,
            json={
                "supplier_id": supplier["id"],
                "items": [
                    {
                        "part_id": (await create_part(owner_client))["id"],
                        "quantity_ordered": 5,
                        "unit_cost": 40.0,
                    }
                ],
            },
        )

        assert response.status_code == 201

    async def test_technician_cannot_see_suppliers(
        self, technician_client: AsyncClient
    ):
        """A technician needs the shelf, not the suppliers behind it."""
        assert (await technician_client.get(SUPPLIERS_URL)).status_code == 403
        assert (await technician_client.get(ORDERS_URL)).status_code == 403

    async def test_technician_cannot_receive(self, technician_client: AsyncClient):
        """Booking in a delivery is a purchasing decision, not a workshop one."""
        assert (
            await technician_client.post(
                f"{ORDERS_URL}2f0b4a1e-0000-4000-8000-000000000000/receive",
                json={"items": [{"item_id": "2f0b4a1e-0000-4000-8000-000000000001", "quantity": 1}]},
            )
        ).status_code == 403

    async def test_service_advisor_cannot_see_orders(
        self, manager_client: AsyncClient
    ):
        """What the shop buys and pays is not an advisor's business."""
        assert (await manager_client.get(SUPPLIERS_URL)).status_code == 403
        assert (await manager_client.get(f"{ORDERS_URL}summary")).status_code == 403

    async def test_customer_is_denied_everything(self, customer_client: AsyncClient):
        """A customer has no business seeing shop costs or suppliers."""
        assert (await customer_client.get(SUPPLIERS_URL)).status_code == 403
        assert (await customer_client.get(ORDERS_URL)).status_code == 403
        assert (await customer_client.get(f"{ORDERS_URL}summary")).status_code == 403

    async def test_unauthenticated_is_rejected(self, unauth_client: AsyncClient):
        assert (await unauth_client.get(SUPPLIERS_URL)).status_code == 401
        assert (await unauth_client.get(ORDERS_URL)).status_code == 401


# --- model rules ------------------------------------------------------------


class TestPurchaseOrderModelRules:
    """Properties and constraints that hold outside the API."""

    async def test_line_arithmetic(self, db: AsyncSession):
        item = PurchaseOrderItem(
            part_id="2f0b4a1e-0000-4000-8000-000000000000",
            line_number=1,
            part_number="MATH-1",
            part_name="Maths part",
            quantity_ordered=12.5,
            quantity_received=4.0,
            unit_cost=8.0,
        )

        assert item.line_total == 100.0
        assert item.received_total == 32.0
        assert item.quantity_outstanding == 8.5
        assert item.is_fully_received is False

        item.quantity_received = 12.5
        assert item.is_fully_received is True
        assert item.quantity_outstanding == 0.0

    async def test_empty_order_is_never_fully_received(self, db: AsyncSession):
        """Nothing arrived, so a line-free order must not close itself."""
        po = PurchaseOrder(
            po_number="PO-EMPTY-TEST",
            supplier_id="2f0b4a1e-0000-4000-8000-000000000000",
            order_date=date.today(),
            status=PurchaseOrderStatus.DRAFT.value,
        )

        assert po.is_fully_received is False
        assert po.is_editable is True
        assert po.is_terminal is False
        assert po.total_units_ordered == 0

    async def test_overdue_only_while_open(self, db: AsyncSession):
        po = PurchaseOrder(
            po_number="PO-LATE-TEST",
            supplier_id="2f0b4a1e-0000-4000-8000-000000000000",
            order_date=date.today() - timedelta(days=10),
            expected_delivery_date=date.today() - timedelta(days=2),
            status=PurchaseOrderStatus.SENT.value,
        )
        assert po.is_overdue is True

        po.status = PurchaseOrderStatus.RECEIVED.value
        assert po.is_overdue is False

    async def test_database_refuses_an_over_receipt(
        self, db: AsyncSession, parts_client: AsyncClient
    ):
        """The not-over constraint is in the schema, not only in the service."""
        supplier = await create_supplier(parts_client)
        part = await create_part(parts_client)
        order = await create_order(parts_client, supplier["id"], part_ids=[part["id"]])
        order = await send_order(parts_client, order["id"])

        stored = (
            await db.execute(select(PurchaseOrderItem).where(PurchaseOrderItem.purchase_order_id == order["id"]))
        ).scalar_one()
        stored.quantity_received = 99.0

        try:
            await db.commit()
            raised = False
        except IntegrityError:
            await db.rollback()
            raised = True
        assert raised is True
