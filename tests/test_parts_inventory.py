"""Tests for the parts catalog and the inventory ledger.

Covers catalog CRUD and search, the transaction-based movement of stock
(receipt / issue / return / adjustment / transfer / scrap), the rules that keep
the balance honest (it is derived, never assigned; it cannot go negative; a
ledger must reconcile with it), low-stock alerts and valuation, and RBAC.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.inventory.models import InventoryTransaction
from app.parts.models import Part

PARTS_URL = "/api/v1/parts/"
INVENTORY_URL = "/api/v1/inventory/"
TRANSACTIONS_URL = "/api/v1/inventory/transactions"

PART_PAYLOAD = {
    "part_number": "BOS0986A",
    "sku": "AF-BR-0001",
    "name": "Brake pad set, front",
    "category": "Brakes",
    "brand": "Brembo",
    "location": "A-1-3",
    "unit_cost": 42.50,
    "unit_price": 79.99,
    "reorder_level": 4,
}


async def create_part(client: AsyncClient, **overrides) -> dict:
    """Add a catalog line, defaulting to the standard test part."""
    payload = {**PART_PAYLOAD, **overrides}
    response = await client.post(PARTS_URL, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def move_stock(
    client: AsyncClient, part_id: str, tx_type: str, quantity: float, **extra
) -> dict:
    """Record one stock movement."""
    response = await client.post(
        TRANSACTIONS_URL,
        json={
            "part_id": part_id,
            "transaction_type": tx_type,
            "quantity": quantity,
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def stock_part(
    client: AsyncClient, part_number: str | None = None, quantity: float = 10, **overrides
) -> dict:
    """Add a catalog line and book stock into it.

    A ``part_number`` gives the line its own identity, so several parts can be
    stocked in one test without colliding on the catalog's unique codes.
    """
    if part_number:
        overrides.setdefault("sku", f"SKU-{part_number}")
        overrides.setdefault("name", f"Part {part_number}")
    else:
        part_number = PART_PAYLOAD["part_number"]
    part = await create_part(client, part_number=part_number, **overrides)
    await move_stock(client, part["id"], "RECEIPT", quantity)
    return part


class TestPartCatalog:
    """Adding, finding, editing and retiring catalog lines."""

    async def test_create_part(self, parts_client: AsyncClient):
        """A new catalog line is created empty and fully priced."""
        part = await create_part(parts_client)

        assert part["part_number"] == "BOS0986A"
        assert part["name"] == "Brake pad set, front"
        assert part["category"] == "Brakes"
        assert part["unit_cost"] == 42.50
        assert part["unit_price"] == 79.99
        assert part["status"] == "ACTIVE"
        assert part["margin"] == 37.49
        assert part["reorder_level"] == 4

    async def test_new_part_starts_empty(self, parts_client: AsyncClient):
        """Stock is never assigned at creation; it arrives by RECEIPT."""
        part = await create_part(parts_client)

        assert part["quantity_on_hand"] == 0
        assert part["is_out_of_stock"] is True
        assert part["stock_status"] == "OUT"

    async def test_opening_balance_is_ignored_on_create(self, parts_client: AsyncClient):
        """A caller cannot smuggle stock onto a new part.

        The balance is derived from the ledger, so an opening quantity has to be
        a RECEIPT like every other movement. Silently accepting one would leave
        the ledger unable to explain the first unit on the shelf.
        """
        response = await parts_client.post(
            PARTS_URL, json={**PART_PAYLOAD, "quantity_on_hand": 50}
        )

        assert response.status_code == 201, response.text
        assert response.json()["quantity_on_hand"] == 0

    async def test_part_numbers_are_normalised(self, parts_client: AsyncClient):
        """Codes are stored uppercase so quoting is case-insensitive."""
        part = await create_part(parts_client, part_number="bos0986a", sku="af-br-0001")

        assert part["part_number"] == "BOS0986A"
        assert part["sku"] == "AF-BR-0001"

    async def test_duplicate_part_number_rejected(self, parts_client: AsyncClient):
        """The same manufacturer part cannot be catalogued twice."""
        await create_part(parts_client)

        response = await parts_client.post(
            PARTS_URL,
            json={
                **PART_PAYLOAD,
                "sku": "AF-BR-0002",
                "name": "Brake pad set, rear",
            },
        )

        assert response.status_code == 409
        assert "already exists" in response.json()["detail"]

    async def test_duplicate_sku_rejected(self, parts_client: AsyncClient):
        """The shop's own code cannot point at two different parts."""
        await create_part(parts_client)

        response = await parts_client.post(
            PARTS_URL,
            json={**PART_PAYLOAD, "part_number": "BOS0987A", "name": "Different part"},
        )

        assert response.status_code == 409

    async def test_create_requires_name_and_category(self, parts_client: AsyncClient):
        """A catalog line without a name or category is not a catalog line."""
        payload = {k: v for k, v in PART_PAYLOAD.items() if k not in ("name", "category")}
        response = await parts_client.post(PARTS_URL, json=payload)

        assert response.status_code == 422

    async def test_create_rejects_negative_price(self, parts_client: AsyncClient):
        """A part cannot be sold at a negative price."""
        response = await parts_client.post(
            PARTS_URL, json={**PART_PAYLOAD, "unit_price": -1}
        )

        assert response.status_code == 422

    async def test_get_part(self, parts_client: AsyncClient):
        """A catalog line is retrievable by ID."""
        part = await create_part(parts_client)

        response = await parts_client.get(f"{PARTS_URL}{part['id']}")

        assert response.status_code == 200
        assert response.json()["id"] == part["id"]

    async def test_get_missing_part_returns_404(self, parts_client: AsyncClient):
        """An unknown part ID is a 404, not an empty response."""
        response = await parts_client.get(
            f"{PARTS_URL}2f0b4a1e-0000-4000-8000-000000000000"
        )

        assert response.status_code == 404

    async def test_list_parts_is_paginated(self, parts_client: AsyncClient):
        """The catalog lists with a real total behind the page."""
        for index in range(5):
            await create_part(
                parts_client,
                part_number=f"PN-{index:04d}",
                sku=f"SKU-{index:04d}",
                name=f"Part number {index}",
            )

        response = await parts_client.get(PARTS_URL, params={"page": 1, "size": 2})

        assert response.status_code == 200
        body = response.json()
        assert len(body["data"]) == 2
        assert body["meta"]["total"] == 5
        assert body["meta"]["pages"] == 3

    async def test_list_filters_by_category(self, parts_client: AsyncClient):
        """Category narrows the catalog."""
        await create_part(parts_client)
        await create_part(
            parts_client,
            part_number="FLT-0001",
            sku="AF-FLT-0001",
            name="Oil filter",
            category="Filters",
        )

        response = await parts_client.get(PARTS_URL, params={"category": "filters"})

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        assert data[0]["category"] == "Filters"

    async def test_list_search(self, parts_client: AsyncClient):
        """Search reaches part number, SKU, name and brand.

        Nobody looks for a part by category when a number is on the quote in
        front of them, so all four have to be findable.
        """
        await create_part(parts_client)
        await create_part(
            parts_client,
            part_number="FLT-0001",
            sku="AF-FLT-0001",
            name="Oil filter",
            category="Filters",
            brand="Mann",
        )

        for term, expected_part_number in (
            ("bos0986", "BOS0986A"),
            ("af-flt", "FLT-0001"),
            ("oil filter", "FLT-0001"),
            ("mann", "FLT-0001"),
        ):
            response = await parts_client.get(PARTS_URL, params={"search": term})
            assert response.status_code == 200, response.text
            numbers = [p["part_number"] for p in response.json()["data"]]
            assert expected_part_number in numbers, term

    async def test_list_filters_out_of_stock(self, parts_client: AsyncClient):
        """`in_stock` separates what is on the shelf from what is not."""
        stocked = await stock_part(parts_client, "PN-STOCK")
        empty = await create_part(
            parts_client,
            part_number="PN-EMPTY",
            sku="SKU-EMPTY",
            name="Empty part",
        )

        response = await parts_client.get(PARTS_URL, params={"in_stock": False})

        assert response.status_code == 200
        numbers = [p["part_number"] for p in response.json()["data"]]
        assert empty["part_number"] in numbers
        assert stocked["part_number"] not in numbers

    async def test_list_filters_low_stock(self, parts_client: AsyncClient):
        """`low_stock` uses the same test as the alert list."""
        await stock_part(parts_client, "PN-LOW", quantity=2, reorder_level=4)
        await stock_part(parts_client, "PN-OK", quantity=20, reorder_level=4)

        response = await parts_client.get(PARTS_URL, params={"low_stock": True})

        assert response.status_code == 200
        numbers = [p["part_number"] for p in response.json()["data"]]
        assert numbers == ["PN-LOW"]

    async def test_list_filters_by_status(self, parts_client: AsyncClient):
        """Retired parts can be filtered out of the catalog view."""
        active = await create_part(parts_client)
        retired = await create_part(
            parts_client, part_number="PN-OLD", sku="SKU-OLD", name="Old part"
        )
        await parts_client.patch(f"{PARTS_URL}{retired['id']}", json={"status": "DISCONTINUED"})

        response = await parts_client.get(PARTS_URL, params={"status": "ACTIVE"})

        assert response.status_code == 200
        numbers = [p["part_number"] for p in response.json()["data"]]
        assert numbers == [active["part_number"]]

    async def test_unknown_status_filter_matches_nothing(
        self, parts_client: AsyncClient
    ):
        """A mistyped filter returns nothing rather than the whole catalog."""
        await create_part(parts_client)

        response = await parts_client.get(PARTS_URL, params={"status": "NOT_A_STATUS"})

        assert response.status_code == 200
        assert response.json()["data"] == []

    async def test_categories(self, parts_client: AsyncClient):
        """Categories in use are listed once each, for filter dropdowns."""
        await create_part(parts_client)
        await create_part(
            parts_client,
            part_number="FLT-0001",
            sku="AF-FLT-0001",
            name="Oil filter",
            category="Filters",
        )

        response = await parts_client.get(f"{PARTS_URL}categories")

        assert response.status_code == 200
        assert response.json() == ["Brakes", "Filters"]

    async def test_update_part_pricing(self, parts_client: AsyncClient):
        """A price change updates the derived margin with it."""
        part = await create_part(parts_client)

        response = await parts_client.patch(
            f"{PARTS_URL}{part['id']}",
            json={"unit_price": 90.00, "unit_cost": 45.00, "reorder_level": 8},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["unit_price"] == 90.00
        assert body["margin"] == 45.00
        assert body["reorder_level"] == 8

    async def test_update_cannot_rename_or_restock(self, parts_client: AsyncClient):
        """Identity and balance are not editable through PATCH.

        Renumbering would orphan the history filed against the old number, and
        the balance belongs to the ledger.
        """
        part = await stock_part(parts_client)

        response = await parts_client.patch(
            f"{PARTS_URL}{part['id']}",
            json={
                "part_number": "RENAMED-1",
                "sku": "RENAMED-SKU",
                "quantity_on_hand": 999,
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["part_number"] == "BOS0986A"
        assert body["sku"] == "AF-BR-0001"
        assert body["quantity_on_hand"] == 10

    async def test_update_rejects_invalid_status(self, parts_client: AsyncClient):
        """An unknown status is rejected rather than stored."""
        part = await create_part(parts_client)

        response = await parts_client.patch(
            f"{PARTS_URL}{part['id']}", json={"status": "GONE"}
        )

        assert response.status_code == 422

    async def test_delete_requires_retirement(self, parts_client: AsyncClient):
        """An active part is retired before it can be deleted."""
        part = await create_part(parts_client)

        response = await parts_client.delete(f"{PARTS_URL}{part['id']}")

        assert response.status_code == 400
        assert "DISCONTINUED" in response.json()["detail"]

    async def test_delete_requires_empty_stock(self, parts_client: AsyncClient):
        """A retired part with stock on the shelf cannot be deleted."""
        part = await stock_part(parts_client)
        await parts_client.patch(f"{PARTS_URL}{part['id']}", json={"status": "DISCONTINUED"})

        response = await parts_client.delete(f"{PARTS_URL}{part['id']}")

        assert response.status_code == 400
        assert "on hand" in response.json()["detail"]

    async def test_delete_preserves_history(self, parts_client: AsyncClient):
        """A part with movements keeps them; deleting it is refused.

        The ledger is the only record of where stock came from, so it outlives
        the catalog line rather than being cascaded away with it.
        """
        part = await stock_part(parts_client)
        await move_stock(parts_client, part["id"], "ISSUE", 10)
        await parts_client.patch(f"{PARTS_URL}{part['id']}", json={"status": "DISCONTINUED"})

        response = await parts_client.delete(f"{PARTS_URL}{part['id']}")

        assert response.status_code == 400
        assert "inventory transaction" in response.json()["detail"]

    async def test_delete_unused_retired_part(self, parts_client: AsyncClient):
        """A retired part that was never stocked goes away cleanly."""
        part = await create_part(parts_client)
        await parts_client.patch(f"{PARTS_URL}{part['id']}", json={"status": "DISCONTINUED"})

        response = await parts_client.delete(f"{PARTS_URL}{part['id']}")

        assert response.status_code == 204
        assert (await parts_client.get(f"{PARTS_URL}{part['id']}")).status_code == 404


class TestInventoryMovements:
    """Every stock movement, and the balance it produces."""

    async def test_receipt_increases_stock(self, parts_client: AsyncClient):
        """A receipt books stock in and records the balance either side."""
        part = await create_part(parts_client)

        movement = await move_stock(parts_client, part["id"], "RECEIPT", 12)

        assert movement["quantity"] == 12
        assert movement["quantity_before"] == 0
        assert movement["quantity_after"] == 12

        current = (await parts_client.get(f"{PARTS_URL}{part['id']}")).json()
        assert current["quantity_on_hand"] == 12
        assert current["stock_status"] == "OK"

    async def test_issue_reduces_stock(self, parts_client: AsyncClient):
        """An issue draws stock off the shelf."""
        part = await stock_part(parts_client, quantity=10)

        movement = await move_stock(parts_client, part["id"], "ISSUE", 4)

        assert movement["quantity"] == -4
        assert movement["quantity_before"] == 10
        assert movement["quantity_after"] == 6

    async def test_issue_can_reference_a_repair_order(
        self, parts_client: AsyncClient, owner_client: AsyncClient, db: AsyncSession
    ):
        """Consumed stock records which job it went to."""
        from app.customers.models import Customer
        from tests.factories import VehicleFactory

        customer = Customer(
            first_name="Inv",
            last_name="Customer",
            email="inv_test@example.com",
            phone="555-0060",
            preferred_contact="EMAIL",
            customer_status="ACTIVE",
        )
        db.add(customer)
        await db.commit()
        await db.refresh(customer)
        vehicle = VehicleFactory.build(customer_id=customer.id)
        db.add(vehicle)
        await db.commit()
        await db.refresh(vehicle)

        ro = await owner_client.post(
            "/api/v1/repair_orders/",
            json={
                "customer_id": str(customer.id),
                "vehicle_id": str(vehicle.id),
                "tasks": [{"description": "Replace pads"}],
            },
        )
        assert ro.status_code == 201, ro.text

        part = await stock_part(parts_client)
        movement = await move_stock(
            parts_client,
            part["id"],
            "ISSUE",
            4,
            repair_order_id=ro.json()["id"],
            reference="RO job",
        )

        assert movement["repair_order_id"] == ro.json()["id"]

    async def test_stock_cannot_go_negative(self, parts_client: AsyncClient):
        """You cannot issue what the shop does not have."""
        part = await stock_part(parts_client, quantity=3)

        response = await parts_client.post(
            TRANSACTIONS_URL,
            json={
                "part_id": part["id"],
                "transaction_type": "ISSUE",
                "quantity": 5,
            },
        )

        assert response.status_code == 400
        assert "cannot go negative" in response.json()["detail"].lower()

        # The failed movement left the balance alone.
        current = (await parts_client.get(f"{PARTS_URL}{part['id']}")).json()
        assert current["quantity_on_hand"] == 3

    async def test_return_increases_stock(self, parts_client: AsyncClient):
        """An issued unit coming back goes back on the shelf."""
        part = await stock_part(parts_client, quantity=5)
        await move_stock(parts_client, part["id"], "ISSUE", 2)

        movement = await move_stock(parts_client, part["id"], "RETURN", 2)

        assert movement["quantity_after"] == 5

    async def test_scrap_requires_a_reason(self, parts_client: AsyncClient):
        """Writing stock off without saying why is not on the record."""
        part = await stock_part(parts_client, quantity=5)

        response = await parts_client.post(
            TRANSACTIONS_URL,
            json={
                "part_id": part["id"],
                "transaction_type": "SCRAP",
                "quantity": 1,
            },
        )

        assert response.status_code == 400
        assert "why" in response.json()["detail"]

    async def test_scrap_with_reason(self, parts_client: AsyncClient):
        """A scrapped unit leaves the shelf and keeps the explanation."""
        part = await stock_part(parts_client, quantity=5)

        movement = await move_stock(
            parts_client, part["id"], "SCRAP", 1, reason="Cracked in the box"
        )

        assert movement["quantity_after"] == 4
        assert movement["reason"] == "Cracked in the box"

    async def test_adjustment_requires_a_direction(self, parts_client: AsyncClient):
        """A stock take correction has to say which way it moved."""
        part = await stock_part(parts_client, quantity=5)

        response = await parts_client.post(
            TRANSACTIONS_URL,
            json={
                "part_id": part["id"],
                "transaction_type": "ADJUSTMENT",
                "quantity": 2,
                "reason": "Two missing at the count",
            },
        )

        assert response.status_code == 422

    async def test_adjustment_out_and_in(self, parts_client: AsyncClient):
        """A correction moves the balance the way it says it does."""
        part = await stock_part(parts_client, quantity=5)

        out = await move_stock(
            parts_client,
            part["id"],
            "ADJUSTMENT",
            2,
            direction="OUT",
            reason="Two missing at the count",
        )
        assert out["quantity"] == -2
        assert out["quantity_after"] == 3

        back_in = await move_stock(
            parts_client,
            part["id"],
            "ADJUSTMENT",
            1,
            direction="IN",
            reason="One turned up in bay 2",
        )
        assert back_in["quantity"] == 1
        assert back_in["quantity_after"] == 4

    async def test_adjustment_requires_a_reason(self, parts_client: AsyncClient):
        """A correction without a reason is not explainable later."""
        part = await stock_part(parts_client, quantity=5)

        response = await parts_client.post(
            TRANSACTIONS_URL,
            json={
                "part_id": part["id"],
                "transaction_type": "ADJUSTMENT",
                "quantity": 2,
                "direction": "OUT",
            },
        )

        assert response.status_code == 400

    async def test_transfer_requires_a_location(self, parts_client: AsyncClient):
        """A bin move has to name the other end of it."""
        part = await stock_part(parts_client, quantity=5)

        response = await parts_client.post(
            TRANSACTIONS_URL,
            json={
                "part_id": part["id"],
                "transaction_type": "TRANSFER",
                "quantity": 2,
                "direction": "OUT",
            },
        )

        assert response.status_code == 422
        assert "location" in response.text.lower()

    async def test_transfer_moves_stock_between_bins(self, parts_client: AsyncClient):
        """The two legs of a move each change the balance they belong to."""
        source = await stock_part(parts_client, "PN-SRC", quantity=8)
        destination = await create_part(
            parts_client, part_number="PN-DST", sku="SKU-DST", name="Destination bin"
        )

        out_leg = await move_stock(
            parts_client, source["id"], "TRANSFER", 3, direction="OUT", to_location="A-2-1"
        )
        in_leg = await move_stock(
            parts_client,
            destination["id"],
            "TRANSFER",
            3,
            direction="IN",
            from_location="A-1-3",
        )

        assert out_leg["quantity_after"] == 5
        assert out_leg["to_location"] == "A-2-1"
        assert in_leg["quantity_after"] == 3
        assert in_leg["from_location"] == "A-1-3"

    async def test_transfer_out_cannot_exceed_stock(self, parts_client: AsyncClient):
        """Moving stock you do not have is still a negative balance."""
        part = await stock_part(parts_client, quantity=2)

        response = await parts_client.post(
            TRANSACTIONS_URL,
            json={
                "part_id": part["id"],
                "transaction_type": "TRANSFER",
                "quantity": 5,
                "direction": "OUT",
                "to_location": "B-1",
            },
        )

        assert response.status_code == 400

    async def test_fractional_quantities(self, parts_client: AsyncClient):
        """Fluid parts move in fractions, and the balance stays exact."""
        part = await create_part(
            parts_client,
            part_number="OIL-5W30",
            sku="SKU-OIL",
            name="Engine oil 5W30",
            category="Fluids",
            unit_cost=8.00,
            unit_price=12.50,
            reorder_level=10,
        )
        await move_stock(parts_client, part["id"], "RECEIPT", 4)
        await move_stock(parts_client, part["id"], "ISSUE", 1.25)
        await move_stock(parts_client, part["id"], "RETURN", 0.25)

        current = (await parts_client.get(f"{PARTS_URL}{part['id']}")).json()
        assert current["quantity_on_hand"] == 3.0

    async def test_direction_must_contradict_nothing(self, parts_client: AsyncClient):
        """A receipt cannot be declared as leaving the building."""
        part = await stock_part(parts_client, quantity=5)

        response = await parts_client.post(
            TRANSACTIONS_URL,
            json={
                "part_id": part["id"],
                "transaction_type": "RECEIPT",
                "quantity": 2,
                "direction": "OUT",
            },
        )

        assert response.status_code == 422

    async def test_discontinued_part_cannot_be_restocked(
        self, parts_client: AsyncClient
    ):
        """A retired part may be drawn down, but never topped up.

        Restocking it would undo the decision to stop trading it, and would do
        it silently from a goods-in.
        """
        part = await stock_part(parts_client, quantity=5)
        await parts_client.patch(f"{PARTS_URL}{part['id']}", json={"status": "DISCONTINUED"})

        receipt = await parts_client.post(
            TRANSACTIONS_URL,
            json={
                "part_id": part["id"],
                "transaction_type": "RECEIPT",
                "quantity": 5,
            },
        )
        assert receipt.status_code == 400
        assert "DISCONTINUED" in receipt.json()["detail"]

        # Drawing down what is already on the shelf is still fine.
        issue = await parts_client.post(
            TRANSACTIONS_URL,
            json={
                "part_id": part["id"],
                "transaction_type": "ISSUE",
                "quantity": 5,
            },
        )
        assert issue.status_code == 201, issue.text
        assert issue.json()["quantity_after"] == 0

    async def test_receipt_cost_is_captured_without_repricing(
        self, parts_client: AsyncClient
    ):
        """A receipt keeps what it cost without moving the catalog price.

        The two are different decisions: what a delivery actually cost is
        history, while what the shop charges for the part is a catalog fact that
        a goods-in should not quietly rewrite.
        """
        part = await create_part(parts_client)

        movement = await move_stock(
            parts_client, part["id"], "RECEIPT", 6, unit_cost=39.99
        )

        assert movement["unit_cost"] == 39.99
        current = (await parts_client.get(f"{PARTS_URL}{part['id']}")).json()
        assert current["unit_cost"] == 42.50

    async def test_receipt_cost_defaults_to_catalog_cost(
        self, parts_client: AsyncClient
    ):
        """A delivery does not have to repeat the price it is charged at."""
        part = await create_part(parts_client)

        movement = await move_stock(parts_client, part["id"], "RECEIPT", 6)

        assert movement["unit_cost"] == 42.50

    async def test_zero_quantity_rejected(self, parts_client: AsyncClient):
        """A movement of nothing is not a movement."""
        part = await stock_part(parts_client)

        response = await parts_client.post(
            TRANSACTIONS_URL,
            json={
                "part_id": part["id"],
                "transaction_type": "RECEIPT",
                "quantity": 0,
            },
        )

        assert response.status_code == 422

    async def test_movement_for_unknown_part_returns_404(
        self, parts_client: AsyncClient
    ):
        """A movement against a part that does not exist is a 404."""
        response = await parts_client.post(
            TRANSACTIONS_URL,
            json={
                "part_id": "2f0b4a1e-0000-4000-8000-000000000000",
                "transaction_type": "RECEIPT",
                "quantity": 1,
            },
        )

        assert response.status_code == 404

    async def test_ledger_reconciles_with_the_balance(
        self, parts_client: AsyncClient, db: AsyncSession
    ):
        """The balance on a part is exactly the sum of its ledger.

        This is the property the whole design rests on: if the two ever
        disagree, no number on the part can be trusted.
        """
        part = await stock_part(parts_client, quantity=10)
        await move_stock(parts_client, part["id"], "ISSUE", 3)
        await move_stock(parts_client, part["id"], "SCRAP", 1, reason="Damaged")
        await move_stock(
            parts_client, part["id"], "ADJUSTMENT", 1, direction="IN", reason="Found"
        )
        await move_stock(parts_client, part["id"], "RETURN", 1)

        current = (await parts_client.get(f"{PARTS_URL}{part['id']}")).json()
        ledger_sum = (
            await db.execute(
                select(func.coalesce(func.sum(InventoryTransaction.quantity), 0.0)).where(
                    InventoryTransaction.part_id == part["id"]
                )
            )
        ).scalar_one()

        assert current["quantity_on_hand"] == ledger_sum
        assert current["quantity_on_hand"] == 8.0

    async def test_every_movement_explains_itself(
        self, parts_client: AsyncClient, db: AsyncSession
    ):
        """Each row carries the balance either side of it."""
        part = await stock_part(parts_client, quantity=10)
        await move_stock(parts_client, part["id"], "ISSUE", 4)

        movements = (
            await db.execute(
                select(InventoryTransaction)
                .where(InventoryTransaction.part_id == part["id"])
                .order_by(InventoryTransaction.created_at.asc())
            )
        ).scalars().all()

        assert [m.transaction_type for m in movements] == ["RECEIPT", "ISSUE"]
        assert movements[0].quantity_before == 0
        assert movements[0].quantity_after == 10
        assert movements[1].quantity_before == 10
        assert movements[1].quantity_after == 6
        # Every movement's "after" is the next one's "before".
        assert movements[0].quantity_after == movements[1].quantity_before


class TestInventoryReads:
    """Reading the ledger, the levels and the alerts."""

    async def test_get_transaction(self, parts_client: AsyncClient):
        """A recorded movement is retrievable on its own."""
        part = await create_part(parts_client)
        movement = await move_stock(parts_client, part["id"], "RECEIPT", 5)

        response = await parts_client.get(f"{TRANSACTIONS_URL}/{movement['id']}")

        assert response.status_code == 200
        assert response.json()["id"] == movement["id"]

    async def test_get_missing_transaction_returns_404(self, parts_client: AsyncClient):
        """An unknown movement is a 404."""
        response = await parts_client.get(
            f"{TRANSACTIONS_URL}/2f0b4a1e-0000-4000-8000-000000000000"
        )

        assert response.status_code == 404

    async def test_list_transactions_filters(self, parts_client: AsyncClient):
        """The ledger can be narrowed to one part or one kind of movement."""
        pads = await stock_part(parts_client, "PN-PADS", quantity=10)
        filters = await stock_part(
            parts_client, "PN-FILT", quantity=5, category="Filters"
        )
        await move_stock(parts_client, pads["id"], "ISSUE", 2)
        await move_stock(
            parts_client, filters["id"], "ISSUE", 1, reason="Not needed"
        )

        by_part = await parts_client.get(
            f"{INVENTORY_URL}transactions", params={"part_id": pads["id"]}
        )
        assert by_part.status_code == 200
        assert by_part.json()["meta"]["total"] == 2

        by_type = await parts_client.get(
            f"{INVENTORY_URL}transactions", params={"transaction_type": "RECEIPT"}
        )
        assert by_type.status_code == 200
        assert by_type.json()["meta"]["total"] == 2

        by_bad_type = await parts_client.get(
            f"{INVENTORY_URL}transactions", params={"transaction_type": "NOPE"}
        )
        assert by_bad_type.status_code == 400

    async def test_transactions_are_newest_first(self, parts_client: AsyncClient):
        """The newest movement is the one you want to see first."""
        part = await stock_part(parts_client, quantity=10)
        await move_stock(parts_client, part["id"], "ISSUE", 2)
        await move_stock(parts_client, part["id"], "ISSUE", 1)

        response = await parts_client.get(
            f"{INVENTORY_URL}transactions", params={"part_id": part["id"]}
        )

        assert response.status_code == 200
        types = [t["transaction_type"] for t in response.json()["data"]]
        assert types[0] == "ISSUE"
        assert types[-1] == "RECEIPT"

    async def test_part_history(self, parts_client: AsyncClient):
        """One part's movements are readable as its history."""
        part = await stock_part(parts_client, quantity=10)
        await move_stock(parts_client, part["id"], "ISSUE", 2)

        response = await parts_client.get(f"{INVENTORY_URL}parts/{part['id']}/history")

        assert response.status_code == 200
        assert len(response.json()) == 2

    async def test_history_for_unknown_part_returns_404(
        self, parts_client: AsyncClient
    ):
        """History for a part that does not exist is a 404."""
        response = await parts_client.get(
            f"{INVENTORY_URL}parts/2f0b4a1e-0000-4000-8000-000000000000/history"
        )

        assert response.status_code == 404

    async def test_low_stock_alerts(self, parts_client: AsyncClient):
        """The buying list is derived from the balance, not stored."""
        await stock_part(parts_client, "PN-OK", quantity=20, reorder_level=4)
        await stock_part(parts_client, "PN-LOW", quantity=2, reorder_level=4)
        await create_part(
            parts_client, part_number="PN-OUT", sku="SKU-OUT", name="Out part"
        )

        response = await parts_client.get(f"{INVENTORY_URL}low-stock")

        assert response.status_code == 200
        alerts = response.json()
        numbers = [a["part_number"] for a in alerts]
        assert "PN-OK" not in numbers
        assert numbers[0] == "PN-OUT"
        assert numbers[-1] == "PN-LOW"

        low = next(a for a in alerts if a["part_number"] == "PN-LOW")
        assert low["shortage"] == 2.0
        assert low["stock_status"] == "LOW"

    async def test_low_stock_clears_after_a_receipt(
        self, parts_client: AsyncClient
    ):
        """An alert disappears as soon as the stock is actually there."""
        part = await stock_part(parts_client, quantity=1, reorder_level=4)
        assert len(
            (await parts_client.get(f"{INVENTORY_URL}low-stock")).json()
        ) == 1

        await move_stock(parts_client, part["id"], "RECEIPT", 10)

        assert (await parts_client.get(f"{INVENTORY_URL}low-stock")).json() == []

    async def test_low_stock_skips_discontinued(self, parts_client: AsyncClient):
        """A part nobody intends to buy more of is not on the buying list."""
        part = await create_part(parts_client)
        await parts_client.patch(f"{PARTS_URL}{part['id']}", json={"status": "DISCONTINUED"})

        default = await parts_client.get(f"{INVENTORY_URL}low-stock")
        assert default.json() == []

        with_retired = await parts_client.get(
            f"{INVENTORY_URL}low-stock", params={"include_discontinued": True}
        )
        assert len(with_retired.json()) == 1

    async def test_stock_levels_value_the_shelf(self, parts_client: AsyncClient):
        """Stock levels report what the shelf is worth."""
        await stock_part(parts_client, quantity=10, reorder_level=4)
        await create_part(parts_client, part_number="OIL-1L", sku="SKU-OIL", name="Oil")

        response = await parts_client.get(f"{INVENTORY_URL}stock-levels")

        assert response.status_code == 200
        levels = response.json()
        pads = next(level for level in levels if level["part_number"] == "BOS0986A")
        assert pads["stock_value"] == 425.00
        assert pads["stock_status"] == "OK"

    async def test_stock_summary(self, parts_client: AsyncClient):
        """The summary counts units, value and how many parts need buying."""
        await stock_part(parts_client, "PN-A", quantity=10, reorder_level=2)
        await stock_part(
            parts_client, "PN-B", quantity=1, reorder_level=5, category="Filters"
        )
        await create_part(parts_client, part_number="PN-C", sku="SKU-C", name="Empty")

        response = await parts_client.get(f"{INVENTORY_URL}stock-summary")

        assert response.status_code == 200
        summary = response.json()
        assert summary["total_parts"] == 3
        assert summary["total_units"] == 11.0
        assert summary["total_stock_value"] == 425.00 + 42.50
        assert summary["low_stock_count"] == 2
        assert summary["out_of_stock_count"] == 1

    async def test_stock_summary_ignores_discontinued(
        self, parts_client: AsyncClient
    ):
        """Stock on a retired line is not counted in the shop's headline."""
        part = await stock_part(parts_client, quantity=10)
        await parts_client.patch(f"{PARTS_URL}{part['id']}", json={"status": "DISCONTINUED"})

        summary = (await parts_client.get(f"{INVENTORY_URL}stock-summary")).json()

        assert summary["total_parts"] == 0
        assert summary["total_units"] == 0.0


class TestPartsInventoryRBAC:
    """Who may see stock, and who may move it."""

    async def test_parts_staff_can_manage_the_catalog(self, parts_client: AsyncClient):
        """Parts staff run the catalog and the ledger."""
        part = await create_part(parts_client)
        movement = await move_stock(parts_client, part["id"], "RECEIPT", 5)

        assert movement["quantity_after"] == 5
        assert (
            await parts_client.get(f"{INVENTORY_URL}low-stock")
        ).status_code == 200

    async def test_technician_can_read_but_not_move_stock(
        self, technician_client: AsyncClient
    ):
        """A technician prices a job from the catalog but does not book stock in."""
        assert (await technician_client.get(PARTS_URL)).status_code == 200
        assert (await technician_client.get(f"{INVENTORY_URL}stock-levels")).status_code == 200

        response = await technician_client.post(
            TRANSACTIONS_URL,
            json={
                "part_id": "2f0b4a1e-0000-4000-8000-000000000000",
                "transaction_type": "RECEIPT",
                "quantity": 1,
            },
        )

        assert response.status_code == 403

    async def test_technician_cannot_add_to_the_catalog(
        self, technician_client: AsyncClient
    ):
        """Only the parts department writes the catalog."""
        response = await technician_client.post(PARTS_URL, json=PART_PAYLOAD)

        assert response.status_code == 403

    async def test_service_advisor_can_read_stock_but_not_move_it(
        self, manager_client: AsyncClient
    ):
        """An advisor quotes from stock levels; the parts department moves it."""
        assert (await manager_client.get(PARTS_URL)).status_code == 200
        assert (await manager_client.get(f"{INVENTORY_URL}stock-levels")).status_code == 200

        response = await manager_client.post(
            TRANSACTIONS_URL,
            json={
                "part_id": "2f0b4a1e-0000-4000-8000-000000000000",
                "transaction_type": "RECEIPT",
                "quantity": 1,
            },
        )

        assert response.status_code == 403

    async def test_customer_is_denied_the_catalog(
        self, customer_client: AsyncClient
    ):
        """A customer has no business seeing shop costs or stock."""
        assert (await customer_client.get(PARTS_URL)).status_code == 403
        assert (
            await customer_client.get(f"{INVENTORY_URL}stock-levels")
        ).status_code == 403
        assert (await customer_client.get(f"{INVENTORY_URL}low-stock")).status_code == 403

    async def test_owner_can_manage_stock(self, owner_client: AsyncClient):
        """The owner inherits every permission."""
        part = await create_part(owner_client)

        movement = await move_stock(owner_client, part["id"], "RECEIPT", 5)

        assert movement["quantity_after"] == 5

        # `parts:manage` is the owner's too, so retirement and deletion are
        # reachable; the deletion is refused on the business rule instead
        # (this part has stock and a ledger), not on the permission.
        assert (
            await owner_client.patch(
                f"{PARTS_URL}{part['id']}", json={"status": "DISCONTINUED"}
            )
        ).status_code == 200
        assert (await owner_client.delete(f"{PARTS_URL}{part['id']}")).status_code == 400

    async def test_unauthenticated_is_rejected(self, unauth_client: AsyncClient):
        """The catalog and the ledger both require a token."""
        assert (await unauth_client.get(PARTS_URL)).status_code == 401
        assert (
            await unauth_client.get(f"{INVENTORY_URL}transactions")
        ).status_code == 401


class TestInventoryModelRules:
    """Properties and constraints that hold outside the API."""

    async def test_margin_and_stock_value(self, db: AsyncSession):
        """Margin is per unit; stock value is the whole shelf."""
        part = Part(
            part_number="DIRECT-1",
            name="Direct part",
            category="Brakes",
            unit_cost=20.0,
            unit_price=35.5,
            quantity_on_hand=3.0,
            reorder_level=1.0,
        )
        db.add(part)
        await db.commit()
        await db.refresh(part)

        assert part.margin == 15.5
        assert part.stock_value == 60.0
        assert part.stock_status == "OK"
        assert part.is_low_stock is False

    async def test_stock_status_boundaries(self, db: AsyncSession):
        """OK above the reorder level, LOW at or below it, OUT at zero."""
        part = Part(
            part_number="BOUND-1",
            name="Boundary part",
            category="Brakes",
            unit_cost=1.0,
            unit_price=2.0,
            quantity_on_hand=5.0,
            reorder_level=5.0,
        )
        db.add(part)
        await db.commit()
        await db.refresh(part)
        assert part.stock_status == "LOW"

        part.quantity_on_hand = 6.0
        await db.commit()
        await db.refresh(part)
        assert part.stock_status == "OK"

        part.quantity_on_hand = 0.0
        await db.commit()
        await db.refresh(part)
        assert part.stock_status == "OUT"
        assert part.is_out_of_stock is True
