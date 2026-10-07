# ruff: noqa: DTZ011
"""Search and filtering across the API's list endpoints.

Every list in a shop application is a filter on top of a table, and the two ways
to get that filter wrong are the same shape: the list answers a question nobody
asked. Too wide -- a search for one customer returning every customer in the
book -- and whoever reads the screen starts acting on somebody else's work. Too
narrow, or filtered by the wrong thing -- a mistyped status quietly returning the
whole list, an overdue filter that also returns bills already paid -- and the
shop stops believing the screen and goes back to a spreadsheet, which is worse
than having no screen.

So every filtered call here is compared against the same endpoint unfiltered. A
filter that does nothing, a filter that hides a row it should have kept, and a
filter that leaks a row it should have dropped all fail the same assertion, and
they fail here rather than on a Monday morning.

Rows that a real visit would have created -- repair orders and their bills --
are written straight to the database instead of being driven through a full
lifecycle, because what is under test is the list endpoint in front of them and
not the sixty requests it takes to reach one paid invoice. Everything else is
created through the API, because creating it is the only way to be sure the
filters are looking at the same rows an operator would see.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.invoices.models import Invoice
from app.repair_orders.models import RepairOrder

CUSTOMERS_URL = "/api/v1/customers/"
VEHICLES_URL = "/api/v1/vehicles/"
REQUESTS_URL = "/api/v1/service_requests/"
ORDERS_URL = "/api/v1/repair_orders/"
INVOICES_URL = "/api/v1/invoices/"
PARTS_URL = "/api/v1/parts/"
INVENTORY_URL = "/api/v1/inventory/transactions"
AUDIT_URL = "/api/v1/audit/"

# Two customers whose details overlap in none of the searchable fields, so a
# filter that returns one of them has actually filtered something.
ADA = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "phone": "555-0142",
    "company_name": "Analytical Engines Ltd",
    "preferred_contact": "EMAIL",
}
BOB = {
    "first_name": "Bob",
    "last_name": "Barker",
    "email": "bob@example.com",
    "phone": "555-0298",
    "preferred_contact": "PHONE",
}


@dataclass
class Shop:
    """Everything the filters will be pointed at."""

    ada: dict
    bob: dict
    ada_car: dict
    bob_car: dict
    brakes: dict
    oil: dict
    requests: dict[str, str] = field(default_factory=dict)
    orders: dict[str, str] = field(default_factory=dict)
    order_numbers: dict[str, str] = field(default_factory=dict)
    invoices: dict[str, str] = field(default_factory=dict)
    invoice_numbers: dict[str, str] = field(default_factory=dict)


def _ok(response, expected: tuple[int, ...] = (200,)) -> dict | list:
    assert response.status_code in expected, f"{response.request.url}: {response.text}"
    return response.json()


async def _page(client: AsyncClient, url: str, **params) -> tuple[list[dict], dict]:
    """One page of a list endpoint, as (rows, meta)."""
    body = _ok(await client.get(url, params=params))
    return body["data"], body["meta"]


def _ids(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows}


async def _create_customer(client: AsyncClient, payload: dict) -> dict:
    return _ok(await client.post(CUSTOMERS_URL, json=payload), (201,))


async def _create_vehicle(
    client: AsyncClient, customer_id: str, *, plate: str, vin: str, make: str, model: str
) -> dict:
    return _ok(
        await client.post(
            VEHICLES_URL,
            json={
                "customer_id": customer_id,
                "vin": vin,
                "license_plate": plate,
                "make": make,
                "model": model,
                "year": 2021,
                "color": "Silver",
                "mileage": 45000,
                "fuel_type": "GASOLINE",
            },
        ),
        (201,),
    )


async def _stock_part(client: AsyncClient, payload: dict, quantity: int) -> dict:
    """Add a catalog line and book stock into it."""
    part = _ok(await client.post(PARTS_URL, json=payload), (201,))
    _ok(
        await client.post(
            INVENTORY_URL,
            json={
                "part_id": part["id"],
                "transaction_type": "RECEIPT",
                "quantity": quantity,
                "reference": f"OPENING-{part['part_number']}",
            },
        ),
        (201,),
    )
    return part


async def _create_request(
    client: AsyncClient, customer_id: str, title: str, priority: str
) -> dict:
    return _ok(
        await client.post(
            REQUESTS_URL,
            json={
                "customer_id": customer_id,
                "title": title,
                "description": f"{title} -- reported at the counter",
                "priority": priority,
            },
        ),
        (201,),
    )


@pytest.fixture()
async def shop(
    db: AsyncSession,
    owner_client: AsyncClient,
    parts_client: AsyncClient,
    manager_client: AsyncClient,
) -> Shop:
    """A small shop: two customers, two cars, two parts, work in progress.

    Two of everything, so that every filter has a right answer and a wrong one.
    """
    ada = await _create_customer(owner_client, ADA)
    bob = await _create_customer(owner_client, BOB)

    ada_car = await _create_vehicle(
        owner_client,
        ada["id"],
        plate="AD-1001",
        vin="1HGBH41JXMN109186",
        make="Toyota",
        model="Corolla",
    )
    bob_car = await _create_vehicle(
        owner_client,
        bob["id"],
        plate="BB-2002",
        vin="2T1B11E5X7C000001",
        make="Ford",
        model="Transit",
    )

    brakes = await _stock_part(
        parts_client,
        {
            "part_number": "SRCH-BRK-01",
            "sku": "SRCH-BRK-01",
            "name": "Brake pad set, front",
            "category": "Brakes",
            "brand": "Brembo",
            "location": "A-1-1",
            "unit_cost": 42.50,
            "unit_price": 79.99,
            "reorder_level": 4,
        },
        10,
    )
    oil = await _stock_part(
        parts_client,
        {
            "part_number": "SRCH-OIL-02",
            "sku": "SRCH-OIL-02",
            "name": "Engine oil 5W-30, 5 litres",
            "category": "Fluids",
            "brand": "Castrol",
            "location": "B-2-4",
            "unit_cost": 18.00,
            "unit_price": 24.50,
            "reorder_level": 6,
        },
        2,
    )

    requests = {
        "ada_high": (await _create_request(manager_client, ada["id"], "Brakes squealing", "HIGH"))["id"],
        "ada_low": (await _create_request(manager_client, ada["id"], "Windscreen chip", "LOW"))["id"],
        "bob_low": (await _create_request(manager_client, bob["id"], "Annual oil change", "LOW"))["id"],
    }

    # Repair orders and their bills, written straight to the database: reaching
    # these through the API means a completed visit per row, and nothing about
    # being written here changes what the list endpoints are being asked to do.
    today = date.today()
    orders: dict[str, str] = {}
    plan = (
        ("ada_open", ada["id"], ada_car["id"], "IN_PROGRESS"),
        ("ada_done", ada["id"], ada_car["id"], "COMPLETED"),
        ("bob_open", bob["id"], bob_car["id"], "IN_PROGRESS"),
        ("bob_quiet", bob["id"], bob_car["id"], "DRAFT"),
    )
    order_rows: dict[str, RepairOrder] = {}
    order_numbers: dict[str, str] = {}
    for index, (key, customer_id, vehicle_id, status) in enumerate(plan, start=1):
        order = RepairOrder(
            ro_number=f"RO-SRCH-{index:02d}",
            customer_id=customer_id,
            vehicle_id=vehicle_id,
            status=status,
        )
        db.add(order)
        await db.flush()
        orders[key] = str(order.id)
        order_numbers[key] = order.ro_number
        order_rows[key] = order
    await db.commit()

    # Four bills in four different states, because "unpaid", "overdue" and "in
    # this period" are three different questions and a shop needs to be able to
    # ask all three of them.
    invoices: dict[str, str] = {}
    invoice_numbers: dict[str, str] = {}
    bills = (
        ("ada_overdue", "INV-SRCH-01", "ada_open", "ISSUED", today - timedelta(days=45), today - timedelta(days=10), 451.94, 0.0),
        ("ada_paid", "INV-SRCH-02", "ada_done", "PAID", today - timedelta(days=60), today - timedelta(days=30), 200.00, 200.00),
        ("bob_open", "INV-SRCH-03", "bob_open", "ISSUED", today, today + timedelta(days=30), 320.00, 0.0),
        ("bob_draft", "INV-SRCH-04", "bob_quiet", "DRAFT", today, today + timedelta(days=30), 75.00, 0.0),
    )
    for key, number, order_key, status, raised, due, total, paid in bills:
        billed = order_rows[order_key]
        invoice = Invoice(
            invoice_number=number,
            customer_id=billed.customer_id,
            vehicle_id=billed.vehicle_id,
            repair_order_id=billed.id,
            status=status,
            invoice_date=raised,
            due_date=due,
            subtotal=total,
            discount_amount=0.0,
            tax_rate=0.0,
            tax_amount=0.0,
            total=total,
            amount_paid=paid,
            paid_at=today if status == "PAID" else None,
        )
        db.add(invoice)
        await db.flush()
        invoices[key] = str(invoice.id)
        invoice_numbers[key] = invoice.invoice_number
    await db.commit()

    return Shop(
        ada=ada,
        bob=bob,
        ada_car=ada_car,
        bob_car=bob_car,
        brakes=brakes,
        oil=oil,
        requests=requests,
        orders=orders,
        order_numbers=order_numbers,
        invoices=invoices,
        invoice_numbers=invoice_numbers,
    )


class TestSearchNarrowsTheList:
    """Searching and filtering, checked against what is really there."""

    @pytest.mark.asyncio
    async def test_a_search_that_matches_nothing_returns_nothing_at_all(
        self, owner_client: AsyncClient, parts_client: AsyncClient, shop: Shop
    ):
        """A dead end must be visibly empty, never a quiet "here is everything".

        The failure this guards against is a mistyped filter being treated as no
        filter at all: the advisor searches a name they half remember, mistypes
        it, and is handed the entire customer list, which reads exactly like a
        search that worked. An empty page is the only answer that cannot be
        mistaken for a successful one.
        """
        # There is something to hide in every one of these lists, so an empty
        # answer is the filter working rather than an empty shop.
        everyone, everyone_meta = await _page(owner_client, CUSTOMERS_URL)
        assert everyone_meta["total"] > 1
        assert len(everyone) == everyone_meta["total"]

        no_such_person, meta = await _page(owner_client, CUSTOMERS_URL, search="zzz-no-such-person")
        assert no_such_person == []
        assert meta["total"] == 0

        no_such_car, car_meta = await _page(owner_client, VEHICLES_URL, search="ZZZ-NOPE")
        assert no_such_car == []
        assert car_meta["total"] == 0

        no_such_part, part_meta = await _page(parts_client, PARTS_URL, search="zzz-nope")
        assert no_such_part == []
        assert part_meta["total"] == 0

        no_such_bill, bill_meta = await _page(owner_client, INVOICES_URL, search="INV-ZZZ")
        assert no_such_bill == []
        assert bill_meta["total"] == 0

    @pytest.mark.asyncio
    async def test_a_mistyped_status_matches_nothing_rather_than_everything(
        self, owner_client: AsyncClient, parts_client: AsyncClient, shop: Shop
    ):
        """An unrecognised status is an empty list, not an absent filter.

        These filters take free text from a dropdown that a browser is free to
        tamper with. Treating an unknown value as "no filter" would hand the
        whole ledger to anyone who asked for a nonsense status.
        """
        all_parts, _ = await _page(parts_client, PARTS_URL)
        assert len(all_parts) == 2

        mistyped_parts, part_meta = await _page(parts_client, PARTS_URL, status="NOT_A_STATUS")
        assert mistyped_parts == []
        assert part_meta["total"] == 0

        all_bills, _ = await _page(owner_client, INVOICES_URL)
        assert len(all_bills) == 4

        mistyped_bills, bill_meta = await _page(owner_client, INVOICES_URL, status="NOT_A_STATUS")
        assert mistyped_bills == []
        assert bill_meta["total"] == 0

    @pytest.mark.asyncio
    async def test_a_customer_is_found_by_whichever_detail_the_caller_remembers(
        self, owner_client: AsyncClient, shop: Shop
    ):
        """One person, four ways of naming them, and never the other person.

        The person on the phone is never the person in the system: it is a last
        name half heard, a company that pays the bill, an email address from ten
        years ago. All four routes have to land on the same customer, and none of
        them may return the customer standing next to them in the list.
        """
        for term, described_as in (
            ("lovel", "half a last name"),
            ("ada@example", "part of an email"),
            ("555-0142", "a phone number"),
            ("Analytical", "the company that pays"),
        ):
            rows, meta = await _page(owner_client, CUSTOMERS_URL, search=term)
            assert _ids(rows) == {shop.ada["id"]}, f"searching by {described_as} found the wrong people"
            assert meta["total"] == 1

        # The status filter is a question about the relationship, not the text,
        # and both customers are active -- so asking for the retired ones must
        # return neither of them.
        active, active_meta = await _page(owner_client, CUSTOMERS_URL, customer_status="ACTIVE")
        assert _ids(active) == {shop.ada["id"], shop.bob["id"]}
        assert active_meta["total"] == 2

        retired, retired_meta = await _page(owner_client, CUSTOMERS_URL, customer_status="INACTIVE")
        assert retired == []
        assert retired_meta["total"] == 0

        # The type-ahead box behind the search field answers the same question
        # and must not leak either: it is the list a user reads while typing.
        typeahead = _ok(
            await owner_client.get(CUSTOMERS_URL + "search", params={"q": "example.com"})
        )
        assert _ids(typeahead) == {shop.ada["id"], shop.bob["id"]}
        assert len(typeahead) == 2

        one = _ok(await owner_client.get(CUSTOMERS_URL + "search", params={"q": "barker"}))
        assert _ids(one) == {shop.bob["id"]}

    @pytest.mark.asyncio
    async def test_a_car_is_found_by_plate_or_vin_or_make_and_stays_with_its_owner(
        self, owner_client: AsyncClient, shop: Shop
    ):
        """A vehicle search finds vehicles, and never crosses a customer line.

        The plate and the VIN are what a customer reads off their own car, and a
        VIN is read aloud and retyped, so the search has to be indifferent to the
        case it arrives in. Scoping to an owner is the other half: the front desk
        wants one customer's cars, not every car that has ever come through.
        """
        for term, described_as in (
            ("AD-1001", "a number plate"),
            ("1hgbh41jxmn109186", "a VIN in the wrong case"),
            ("corolla", "a model"),
            ("toyota", "a make"),
        ):
            rows, meta = await _page(owner_client, VEHICLES_URL, search=term)
            assert _ids(rows) == {shop.ada_car["id"]}, f"searching by {described_as} found the wrong car"
            assert meta["total"] == 1

        other_car, _ = await _page(owner_client, VEHICLES_URL, search="transit")
        assert _ids(other_car) == {shop.bob_car["id"]}

        # Ada's cars, which is one, and Bob's, which is one.
        ada_cars, ada_meta = await _page(owner_client, VEHICLES_URL, customer_id=shop.ada["id"])
        assert _ids(ada_cars) == {shop.ada_car["id"]}
        assert ada_meta["total"] == 1

        # Owner and text together are two conditions, not two alternatives: Bob's
        # car does not appear in Ada's list however well it matches the text.
        crossed, crossed_meta = await _page(
            owner_client,
            VEHICLES_URL,
            customer_id=shop.ada["id"],
            search="transit",
        )
        assert crossed == []
        assert crossed_meta["total"] == 0

        typeahead = _ok(
            await owner_client.get(VEHICLES_URL + "search", params={"q": "BB-2002"})
        )
        assert _ids(typeahead) == {shop.bob_car["id"]}

    @pytest.mark.asyncio
    async def test_filters_given_together_mean_both_at_once(
        self, owner_client: AsyncClient, parts_client: AsyncClient, manager_client: AsyncClient, shop: Shop
    ):
        """Two filters narrow twice; neither one replaces the other.

        This is the failure that produces a confident wrong answer: the desk
        picks "Brakes" from one dropdown, types "Castrol" into the search box,
        and gets back a list of brake pads even though the only Castrol line in
        the shop is oil. Each filter passes on its own; only a test that asks for
        both can catch the pair that quietly means "either".
        """
        brakes_only, brakes_meta = await _page(parts_client, PARTS_URL, category="Brakes")
        assert _ids(brakes_only) == {shop.brakes["id"]}
        assert brakes_meta["total"] == 1

        castrol_only, _ = await _page(parts_client, PARTS_URL, search="Castrol")
        assert _ids(castrol_only) == {shop.oil["id"]}

        # The category and the brand are both real, and they are on different
        # parts. Asking for both must find nothing rather than either one.
        contradiction, contradiction_meta = await _page(
            parts_client, PARTS_URL, category="Brakes", search="Castrol"
        )
        assert contradiction == []
        assert contradiction_meta["total"] == 0

        # The same shape on the money side: Ada's overdue bill is issued, so the
        # status matches; Bob's id does not, so the pair must come back empty.
        crossed_bill, crossed_meta = await _page(
            owner_client,
            INVOICES_URL,
            status="ISSUED",
            customer_id=shop.bob["id"],
            search=shop.invoice_numbers["ada_overdue"],
        )
        assert crossed_bill == []
        assert crossed_meta["total"] == 0

        # And on the request queue: one of Bob's requests is HIGH-priority-free,
        # so asking for Bob's high-priority work returns nothing, not his other
        # request under a filter that was quietly ignored.
        bob_high, bob_high_meta = await _page(
            manager_client, REQUESTS_URL, customer_id=shop.bob["id"], priority="HIGH"
        )
        assert bob_high == []
        assert bob_high_meta["total"] == 0

        ada_high, ada_high_meta = await _page(
            manager_client, REQUESTS_URL, customer_id=shop.ada["id"], priority="HIGH"
        )
        assert _ids(ada_high) == {shop.requests["ada_high"]}
        assert ada_high_meta["total"] == 1

    @pytest.mark.asyncio
    async def test_the_count_and_the_page_describe_the_filtered_set(
        self, owner_client: AsyncClient, shop: Shop
    ):
        """Pagination metadata belongs to the filtered list, and pages never repeat.

        A count that ignored the filter is how a list ends up showing "1-20 of 47"
        over three results, and a page boundary that repeated a row is how the
        same repair order gets worked twice.
        """
        rows, meta = await _page(owner_client, CUSTOMERS_URL, search="example.com", size=1, page=1)
        assert len(rows) == 1
        assert meta == {"page": 1, "size": 1, "total": 2, "pages": 2}

        second, second_meta = await _page(owner_client, CUSTOMERS_URL, search="example.com", size=1, page=2)
        assert len(second) == 1
        assert second_meta == {"page": 2, "size": 1, "total": 2, "pages": 2}

        # Two rows, two pages, no overlap: the customer on page 1 is not the
        # customer on page 2.
        assert _ids(rows).isdisjoint(_ids(second))
        assert _ids(rows) | _ids(second) == {shop.ada["id"], shop.bob["id"]}

        # A third page past the end is empty rather than a repeat of the last one.
        beyond, beyond_meta = await _page(
            owner_client, CUSTOMERS_URL, search="example.com", size=1, page=3
        )
        assert beyond == []
        assert beyond_meta["total"] == 2

        # The count is of the filtered set, not of the table.
        bills, bills_meta = await _page(owner_client, INVOICES_URL, status="PAID")
        assert _ids(bills) == {shop.invoices["ada_paid"]}
        assert bills_meta["total"] == 1

    @pytest.mark.asyncio
    async def test_the_money_filters_answer_three_different_questions(
        self, owner_client: AsyncClient, shop: Shop
    ):
        """"Unpaid", "overdue" and "raised in this period" are three questions.

        They overlap and none of them contains another. A bill can be unpaid and
        not yet due, unpaid and long overdue, or paid and closed; only the last
        two are ever a reason to chase someone. Reporting them as one number is
        how a garage ends up chasing a customer who paid on Monday.
        """
        all_bills, all_meta = await _page(owner_client, INVOICES_URL)
        assert _ids(all_bills) == set(shop.invoices.values())
        assert all_meta["total"] == 4

        # Unpaid: asked for, not yet settled. A draft has not been asked for and
        # a paid bill is closed, so neither belongs in the collection run.
        unpaid, unpaid_meta = await _page(owner_client, INVOICES_URL, unpaid_only="true")
        assert _ids(unpaid) == {
            shop.invoices["ada_overdue"],
            shop.invoices["bob_open"],
        }
        assert unpaid_meta["total"] == 2
        assert {row["status"] for row in unpaid} == {"ISSUED"}

        # Overdue: unpaid *and* past the promised date. Bob's bill is unpaid but
        # not yet due; Ada's paid bill is long past a due date and is closed.
        overdue, overdue_meta = await _page(owner_client, INVOICES_URL, overdue_only="true")
        assert _ids(overdue) == {shop.invoices["ada_overdue"]}
        assert overdue_meta["total"] == 1
        assert overdue[0]["is_overdue"] is True
        assert overdue[0]["days_overdue"] > 0

        # Free text on the bill number: the number is on the customer's copy, so
        # it is what gets read over the phone, in whatever case they read it.
        by_number, by_number_meta = await _page(
            owner_client, INVOICES_URL, search=shop.invoice_numbers["bob_open"].lower()
        )
        assert _ids(by_number) == {shop.invoices["bob_open"]}
        assert by_number_meta["total"] == 1

        # A period bounds the flow, and only the flow: today's bills are the two
        # raised today, and the two raised weeks and months ago are outside it.
        today = date.today()
        today_only, today_meta = await _page(
            owner_client,
            INVOICES_URL,
            start_date=str(today),
            end_date=str(today),
        )
        assert _ids(today_only) == {
            shop.invoices["bob_open"],
            shop.invoices["bob_draft"],
        }
        assert today_meta["total"] == 2

        # Overdue and a period together still mean both: the only bill that is
        # past its date was also raised before the window, and it is returned
        # only when the window is not asked for.
        overdue_this_week, week_meta = await _page(
            owner_client,
            INVOICES_URL,
            overdue_only="true",
            start_date=str(today - timedelta(days=7)),
            end_date=str(today),
        )
        assert overdue_this_week == []
        assert week_meta["total"] == 0

    @pytest.mark.asyncio
    async def test_a_work_queue_for_one_person_never_shows_another_persons_work(
        self, owner_client: AsyncClient, shop: Shop
    ):
        """The shop's order list, narrowed to a customer or a car, stays theirs.

        This is the screen a service advisor works from: what is open on this
        customer's car. A row belonging to somebody else is not a cosmetic
        problem -- it is another customer's registration, mileage and fault
        description, on a screen the advisor is looking at to answer Ada's call.
        """
        all_orders, all_meta = await _page(owner_client, ORDERS_URL)
        assert _ids(all_orders) == set(shop.orders.values())
        assert all_meta["total"] == 4

        ada_orders, ada_meta = await _page(owner_client, ORDERS_URL, customer_id=shop.ada["id"])
        assert _ids(ada_orders) == {shop.orders["ada_open"], shop.orders["ada_done"]}
        assert ada_meta["total"] == 2
        assert shop.orders["bob_open"] not in _ids(ada_orders)

        # Status narrows within the customer rather than replacing the customer.
        in_progress, in_progress_meta = await _page(
            owner_client, ORDERS_URL, status="IN_PROGRESS"
        )
        assert _ids(in_progress) == {shop.orders["ada_open"], shop.orders["bob_open"]}
        assert in_progress_meta["total"] == 2

        ada_in_progress, both_meta = await _page(
            owner_client,
            ORDERS_URL,
            customer_id=shop.ada["id"],
            status="IN_PROGRESS",
        )
        assert _ids(ada_in_progress) == {shop.orders["ada_open"]}
        assert both_meta["total"] == 1

        # Same from the other direction: the car, and the car plus a status it
        # cannot have.
        by_car, car_meta = await _page(
            owner_client, ORDERS_URL, vehicle_id=shop.ada_car["id"]
        )
        assert _ids(by_car) == {shop.orders["ada_open"], shop.orders["ada_done"]}
        assert car_meta["total"] == 2

        impossible, impossible_meta = await _page(
            owner_client,
            ORDERS_URL,
            vehicle_id=shop.ada_car["id"],
            status="DRAFT",
        )
        assert impossible == []
        assert impossible_meta["total"] == 0

        # The bill hangs off exactly one order, so filtering bills by order is
        # the last narrowing step and it lands on a single row.
        one_bill, one_bill_meta = await _page(
            owner_client, INVOICES_URL, repair_order_id=shop.orders["ada_open"]
        )
        assert _ids(one_bill) == {shop.invoices["ada_overdue"]}
        assert one_bill_meta["total"] == 1

    @pytest.mark.asyncio
    async def test_the_stock_alert_lists_the_parts_that_actually_need_ordering(
        self, parts_client: AsyncClient, shop: Shop
    ):
        """"Order more of this" is a low-stock question, not a category one.

        The parts clerk reads this list to spend money. If it showed every brake
        pad in the shop the clerk would learn to ignore it; if it missed the line
        that is actually below its reorder level the shop would stop the job when
        it needed the part most.
        """
        everything, _ = await _page(parts_client, PARTS_URL)
        assert len(everything) == 2

        low, low_meta = await _page(parts_client, PARTS_URL, low_stock="true")
        # The oil was stocked two against a reorder level of six.
        assert _ids(low) == {shop.oil["id"]}
        assert low_meta["total"] == 1

        healthy, healthy_meta = await _page(parts_client, PARTS_URL, low_stock="false")
        assert _ids(healthy) == {shop.brakes["id"]}
        assert healthy_meta["total"] == 1

        # Both parts are in stock, so the in-stock filter is not what separates
        # them -- which is exactly why the low-stock flag has to be its own test.
        in_stock, in_stock_meta = await _page(parts_client, PARTS_URL, in_stock="true")
        assert _ids(in_stock) == {shop.brakes["id"], shop.oil["id"]}
        assert in_stock_meta["total"] == 2

    @pytest.mark.asyncio
    async def test_the_trail_can_be_read_one_object_at_a_time(
        self, owner_client: AsyncClient, shop: Shop
    ):
        """The audit log has to be able to answer "what happened to this one".

        Every other list in the shop is a queue to work through; this one is read
        when something has gone wrong with a specific object, and the answer has
        to be that object's history and nothing else. An unfiltered log is the
        fallback, and a filter that quietly returned the whole log would be worse
        than no filter at all, because it looks like an answer.
        """
        whole_log, whole_meta = await _page(owner_client, AUDIT_URL, size=200)
        assert whole_meta["total"] > 0
        # Both customers are in the unfiltered trail, so the per-object filters
        # below are narrowing it rather than merely being the only thing in it.
        whole_entities = {entry["entity_id"] for entry in whole_log}
        assert {shop.ada["id"], shop.bob["id"]} <= whole_entities

        ada_log, ada_meta = await _page(
            owner_client,
            AUDIT_URL,
            entity_type="customer",
            entity_id=shop.ada["id"],
            size=200,
        )
        assert ada_meta["total"] >= 1
        assert {entry["entity_id"] for entry in ada_log} == {shop.ada["id"]}
        assert {entry["entity_type"] for entry in ada_log} == {"customer"}
        assert {entry["action"] for entry in ada_log} == {"CREATE"}

        # Ada's trail is a strict subset of the whole log.
        assert ada_meta["total"] < whole_meta["total"]
        bob_entries = _ok(
            await owner_client.get(
                AUDIT_URL,
                params={"entity_type": "customer", "entity_id": shop.bob["id"], "size": 200},
            )
        )
        assert bob_entries["meta"]["total"] == 1
        assert {entry["entity_id"] for entry in bob_entries["data"]} == {shop.bob["id"]}

        # Asking for an action the customer never had: nothing, rather than the
        # whole trail again.
        deleted, deleted_meta = await _page(
            owner_client,
            AUDIT_URL,
            entity_type="customer",
            entity_id=shop.ada["id"],
            action="DELETE",
        )
        assert deleted == []
        assert deleted_meta["total"] == 0

        # A window that has not happened yet cannot contain today's entries.
        tomorrow, tomorrow_meta = await _page(
            owner_client,
            AUDIT_URL,
            date_from=str(date.today() + timedelta(days=1)),
        )
        assert tomorrow == []
        assert tomorrow_meta["total"] == 0
