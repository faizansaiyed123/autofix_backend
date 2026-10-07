"""Realistic demo data, so a fresh database looks like a shop that has been running.

**Demo data is separate from RBAC data, and deliberately so.** ``seed_all`` plants
roles, permissions and the five demo logins — the minimum a developer needs to
authenticate at all, and the only thing the test suite seeds. This module plants
the *business*: customers, their cars, work in progress, money owed and money
taken. Keeping them apart means the test suite's database is not full of a shop's
worth of rows that every counting test then has to subtract, and it means a
developer can ask for one without the other.

**Everything is written through the models, not the services.** The services
commit internally, which would make this function impossible to roll back and
impossible to test. Writing the rows directly means the whole dataset is one
transaction: a failure halfway through leaves a database that is either
completely seeded or not seeded at all, never one with a repair order whose
customer does not exist.

**The money has to add up.** Reports sum these rows and are believed. Line
totals, subtotals, tax, invoice totals and amounts paid are computed with the
same helpers the services use, so a demo invoice is one a customer could
genuinely have been sent, and the revenue report's "billed versus collected"
distinction is visible in seed data rather than only in fixtures.

**The data is chosen to make every report interesting.** A shop where everything
is paid on the day it is due cannot show what "overdue" looks like; one where every
customer has one invoice cannot show retention. So there is a paid bill, a
part-paid one, an overdue one and a draft, a delivered order and one still on the
lift, a customer with a login (who can therefore sign in and see the portal) and
four who never made an account.

**Stock arrives through the ledger.** ``quantity_on_hand`` is a running total
nobody edits, so the opening balance is a ``RECEIPT`` like any other delivery and
the parts fitted to a repair order are ``ISSUE``d off it. A seed that set the
balance directly would contradict the rule the inventory module is built on, and
the demo could not answer "where did this come from".

**The attention centres have something in them.** Notifications are written by
services when they act, so seeding only through the models would leave every
badge reading zero. The notices here are the ones the seeded history implies, each
addressed to whoever the record is actually about.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.appointments.models import Appointment, AppointmentStatus
from app.auth.models import User
from app.checkins.models import CheckIn
from app.customers.models import Customer, CustomerStatus
from app.estimates.models import (
    Estimate,
    EstimateItem,
    EstimateItemType,
    EstimateStatus,
    round_money,
)
from app.inspections.models import Inspection, InspectionItem, InspectionStatus
from app.inventory.models import InventoryTransaction, InventoryTransactionType
from app.invoices.models import Invoice, InvoiceItem, InvoiceStatus
from app.invoices.models import round_money as invoice_round
from app.labor.models import LaborRecord
from app.notifications.events import NotificationEvent
from app.notifications.models import PRIORITY_HIGH, Notification, NotificationType
from app.notifications.services import invoice_issued_event
from app.parts.models import Part
from app.payments.models import Payment, PaymentStatus
from app.repair_orders.models import (
    RepairOrder,
    RepairOrderStatus,
    RepairTask,
    RepairTaskStatus,
)
from app.service_requests.models import ServiceRequest
from app.suppliers.models import Supplier
from app.vehicles.models import Vehicle

logger = logging.getLogger("autofix.seed.demo")

# The customer's own login, so the portal has somebody to be for. Matched by
# email rather than hard-coded id, because the id is generated.
DEMO_CUSTOMER_EMAIL = "customer@autofix.demo"

# Everyone else who appears in the data below, by role.
DEMO_OWNER_EMAIL = "owner@autofix.demo"
DEMO_ADVISOR_EMAIL = "manager@autofix.demo"
DEMO_TECH_EMAIL = "tech@autofix.demo"
DEMO_PARTS_EMAIL = "parts@autofix.demo"

TAX_RATE = 0.0825


# --- the catalog -------------------------------------------------------------

SUPPLIERS: list[dict] = [
    {
        "name": "Ridgeline Auto Parts",
        "contact_name": "Tom Whitfield",
        "email": "orders@ridgelineparts.test",
        "phone": "+1-503-555-0118",
        "city": "Portland",
        "state": "OR",
        "account_number": "RL-88213",
        "lead_time_days": 2,
        "payment_terms": "NET_30",
        "is_preferred": True,
    },
    {
        "name": "Northgate Motor Supply",
        "contact_name": "Priya Raman",
        "email": "sales@northgatemotor.test",
        "phone": "+1-503-555-0144",
        "city": "Vancouver",
        "state": "WA",
        "account_number": "NG-40551",
        "lead_time_days": 4,
        "payment_terms": "NET_15",
        "is_preferred": False,
    },
    {
        "name": "Brake & Suspension Direct",
        "contact_name": "Alan Hsu",
        "email": "hello@brakesuspension.test",
        "phone": "+1-971-555-0190",
        "city": "Salem",
        "state": "OR",
        "lead_time_days": 6,
        "payment_terms": "NET_30",
        "is_preferred": False,
    },
]

# (part_number, name, category, unit_cost, unit_price, on_hand, reorder_level, brand)
PARTS: list[tuple[str, str, str, float, float, int, int, str]] = [
    ("BRK-PAD-1042", "Ceramic brake pad set, front", "BRAKES", 38.50, 89.00, 14, 6, "Ferodo"),
    ("BRK-ROT-2210", "Vented brake rotor, front pair", "BRAKES", 62.00, 149.00, 8, 4, "Brembo"),
    ("FLT-OIL-3310", "Premium oil filter", "FILTERS", 7.25, 18.50, 42, 20, "Bosch"),
    ("FLT-AIR-3320", "Engine air filter", "FILTERS", 11.00, 26.00, 18, 10, "Bosch"),
    ("FLT-CAB-3401", "Cabin air filter", "FILTERS", 9.40, 24.00, 5, 12, "Denso"),
    ("BAT-AGM-7700", "AGM battery group 48", "ELECTRICAL", 189.00, 329.00, 3, 3, "Varta"),
    ("OIL-5W30-5Q", "Synthetic 5W-30, 5 quarts", "LUBRICANTS", 24.00, 44.00, 26, 12, "Castrol"),
    ("WIP-BLADE-5501", "Beam wiper blade, 22 inch", "WIPERS", 11.80, 29.95, 4, 8, "Rain-X"),
    ("BELT-SERP-9010", "Serpentine belt", "ENGINE", 29.00, 74.00, 7, 4, "Gates"),
    ("COOL-COOL-2200", "Long-life coolant, 1 gallon", "FLUIDS", 18.00, 39.00, 11, 6, "Peak"),
]

# Stock consumed by the repair orders seeded below: (part_number, order_index,
# quantity, reason). ``order_index`` is the position of the order in
# ``_seed_repair_orders`` — 0 delivered brakes, 3 the stale battery-and-brakes job,
# 4 the half-settled service. Parts are *fitted* to work, so the ledger says which
# order took them; a demo catalog where every unit appeared out of nowhere would
# make the shop look like it conjures brake pads.
#
# The opening receipt in ``_seed_inventory_ledger`` carries these quantities too,
# so a part's closing balance is the figure above and its history is real.
ISSUES: list[tuple[str, int, float, str]] = [
    ("BRK-PAD-1042", 0, 1.0, "Fitted on the front brake job."),
    ("FLT-OIL-3310", 4, 1.0, "Fitted on the oil and filter change."),
    ("OIL-5W30-5Q", 4, 1.0, "Five quarts drained and replaced."),
    ("BRK-ROT-2210", 3, 1.0, "Fitted on the full brake service."),
    ("BAT-AGM-7700", 3, 1.0, "Fitted on the battery replacement."),
    ("FLT-CAB-3401", 5, 1.0, "Fitted on the cabin filter change."),
    ("WIP-BLADE-5501", 5, 1.0, "Fitted on the wiper change."),
    ("FLT-OIL-3310", 6, 1.0, "Fitted on the returning customer's service."),
    ("FLT-AIR-3320", 6, 1.0, "Fitted on the returning customer's service."),
]

# (first, last, company, email, phone, linked_to_portal, preferred_contact)
CUSTOMERS: list[tuple[str, str, str | None, str, str, bool, str]] = [
    ("David", "Wilson", None, DEMO_CUSTOMER_EMAIL, "+1-503-555-0101", True, "EMAIL"),
    ("Amara", "Okafor", None, "amara.okafor@customer.test", "+1-503-555-0102", False, "PHONE"),
    ("Grace", "Lindqvist", "Lindqvist Design Studio", "grace@studio.test", "+1-503-555-0103", False, "EMAIL"),
    ("Hector", "Salazar", "Salazar Landscaping", "hector@salazarlandscaping.test", "+1-971-555-0104", False, "EMAIL"),
    ("Yuki", "Tanaka", None, "yuki.tanaka@customer.test", "+1-503-555-0105", False, "SMS"),
    ("Bernard", "Achebe", "Achebe & Sons Delivery", "bernard@achebedelivery.test", "+1-971-555-0106", False, "PHONE"),
]

# (customer_index, vin, plate, make, model, year, mileage, fuel, status, color)
VEHICLES: list[tuple[int, str, str, str, str, int, int, str, str, str]] = [
    (0, "1HGCM82633A004352", "PDX-1182", "Honda", "Accord", 2019, 74210, "GASOLINE", "ACTIVE", "Silver"),
    (0, "5YJ3E1EA7KF317250", "EV-2044", "Tesla", "Model 3", 2019, 38640, "ELECTRIC", "ACTIVE", "White"),
    (1, "3VW2K7AJ9FM288211", "PDX-3390", "Volkswagen", "Jetta", 2015, 118902, "GASOLINE", "ACTIVE", "Blue"),
    (2, "1FTEW1EP7JKD90218", "PDX-5517", "Ford", "F-150", 2018, 96540, "GASOLINE", "ACTIVE", "Grey"),
    (3, "2T1BURHE5KC214770", "PDX-7761", "Toyota", "Corolla", 2019, 61220, "GASOLINE", "ACTIVE", "Red"),
    (4, "JTDKN3DU8A0123456", "PDX-9028", "Toyota", "Prius", 2017, 88415, "HYBRID", "ACTIVE", "Green"),
    (5, "1GCVKREC5EZ221190", "PDX-1403", "Chevrolet", "Silverado", 2016, 154780, "GASOLINE", "ACTIVE", "Black"),
    (1, "WP0ZZZ99ZTS392124", "PDX-6635", "Porsche", "Cayenne", 2014, 103300, "GASOLINE", "ACTIVE", "Brown"),
]

# (customer_index, title, description, priority, status, vehicle_index)
SERVICE_REQUESTS: list[tuple[int, str, str, str, str, int | None]] = [
    (0, "Grinding when braking", "Rear brakes grind over 30mph, worse when cold.", "HIGH", "CONVERTED", 0),
    (2, "Annual service", "Routine 60k service if the car is going in anyway.", "STANDARD", "CONVERTED", 3),
    (4, "Check engine light", "Check engine light came on after filling up.", "STANDARD", "IN_REVIEW", 5),
    (5, "Towing quote", "Needs a second opinion on a transmission estimate.", "LOW", "NEW", 6),
    # The portal customer's outstanding request. Without it the demo account has
    # nothing waiting on it, and the portal is the one screen that cannot show an
    # empty state usefully.
    (0, "Charging has slowed", "10-80% used to take 25 minutes and now takes 40.", "STANDARD", "IN_REVIEW", 1),
]

# (customer_index, vehicle_index, request_index, service_type, status, days_from_now, hour, concern)
APPOINTMENTS: list[tuple[int, int, int | None, str, str, int, int, str]] = [
    (0, 0, 0, "BRAKE_SERVICE", "COMPLETED", -21, 9, "Grinding noise from the front wheels."),
    (2, 3, 1, "SCHEDULED_MAINTENANCE", "COMPLETED", -9, 10, "60k service, no other complaints."),
    (4, 5, 2, "DIAGNOSTIC", "CONFIRMED", 2, 14, "Check engine light, no obvious loss of power."),
    (3, 4, None, "TIRE_SERVICE", "REQUESTED", 5, 11, "Tires at 3mm, would like a quote first."),
    (0, 1, 4, "DIAGNOSTIC", "REQUESTED", 7, 9, "Charge speed has dropped off since the update."),
]

# (customer_index, vehicle_index, appointment_index, odometer, checkin_type, status, notes)
CHECK_INS: list[tuple[int, int, int, int, str, str, str]] = [
    (0, 0, 0, 73980, "APPOINTMENT", "COMPLETED", "Customer reported brake noise since 40k."),
    (2, 3, 1, 96480, "APPOINTMENT", "COMPLETED", "Walked the lot with the service manager."),
]


# --- helpers -----------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _at(days: int, hour: int = 10) -> datetime:
    """A timestamp ``days`` from today, at a plausible working hour."""
    moment = _now() + timedelta(days=days)
    return moment.replace(hour=hour, minute=0, second=0, microsecond=0)


def _money(value: float) -> float:
    """Round the way the domain does, so demo totals match service totals.

    Two modules define their own ``round_money``; using the invoice one here
    means a demo invoice is byte-for-byte what the service would have produced.
    """
    return invoice_round(value)


def _estimate_totals(estimate: Estimate, items: list[EstimateItem]) -> None:
    """Recompute an estimate's stored totals from its lines.

    The same arithmetic as ``estimates.services.recalculate``, inlined because
    the service version commits and this must not. The lines are passed in
    rather than read from ``estimate.items``, which would need a lazy load in an
    async session.
    """
    charges = 0.0
    discounts = 0.0
    for item in items:
        item.line_total = item.compute_line_total()
        if item.item_type == EstimateItemType.DISCOUNT.value:
            discounts += abs(item.line_total)
        else:
            charges += item.line_total
    subtotal = round_money(charges)
    discount_amount = min(round_money(discounts), subtotal)
    taxable = round_money(max(subtotal - discount_amount, 0.0))
    tax_amount = round_money(taxable * float(estimate.tax_rate))
    estimate.subtotal = subtotal
    estimate.discount_amount = discount_amount
    estimate.tax_amount = tax_amount
    estimate.total = round_money(taxable + tax_amount)


def _invoice_totals(invoice: Invoice, items: list[InvoiceItem]) -> None:
    """Recompute an invoice's stored totals from its lines."""
    charges = 0.0
    discounts = 0.0
    for item in items:
        item.line_total = _line_total(item)
        if item.item_type == "DISCOUNT":
            discounts += abs(item.line_total)
        else:
            charges += item.line_total
    subtotal = _money(charges)
    discount_amount = min(_money(discounts), subtotal)
    taxable = _money(max(subtotal - discount_amount, 0.0))
    tax_amount = _money(taxable * float(invoice.tax_rate))
    invoice.subtotal = subtotal
    invoice.discount_amount = discount_amount
    invoice.tax_amount = tax_amount
    invoice.total = _money(taxable + tax_amount)


def _line_total(item: InvoiceItem) -> float:
    gross = float(item.quantity or 0.0) * float(item.unit_price or 0.0)
    net = gross - float(item.discount_amount or 0.0)
    return _money(max(net, 0.0))


@dataclass
class DemoDataResult:
    """What a seeding run actually did, for the CLI to report and tests to assert."""

    created: bool
    counts: dict[str, int] = field(default_factory=dict)
    marker_customer_id: str | None = None

    def __str__(self) -> str:
        if not self.created:
            return "Demo data already present; nothing to do"
        summary = ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))
        return f"Seeded demo data: {summary}"


# --- the seed ----------------------------------------------------------------


async def seed_demo_data(session: AsyncSession) -> DemoDataResult:
    """Write a realistic week of a shop's work.

    Idempotent: if a customer carrying the demo marker already exists, this
    returns ``created=False`` and writes nothing. A seed script that is run twice
    — which every developer does at least once — must not double every invoice.

    Commits nothing. The caller decides, so the whole dataset is one
    transaction and a failure part-way through leaves nothing behind.
    """
    if await _already_seeded(session):
        logger.info("Demo data already present; skipping")
        return DemoDataResult(created=False)

    users = await _users_by_email(session)
    customers = await _seed_customers(session, users)
    vehicles = await _seed_vehicles(session, customers)
    parts = await _seed_catalog(session)
    requests = await _seed_service_requests(session, customers, vehicles)
    # Check-ins come before appointments because the link is one-way: an
    # appointment records which check-in it belongs to, not the other way round.
    checkins = await _seed_check_ins(session, customers, vehicles, users)
    appointments = await _seed_appointments(
        session, customers, vehicles, users, requests, checkins
    )
    inspections = await _seed_inspections(
        session, customers, vehicles, users, checkins
    )
    estimates = await _seed_estimates(
        session, customers, vehicles, users, requests, inspections, parts
    )
    orders = await _seed_repair_orders(
        session, customers, vehicles, users, appointments, estimates
    )
    await _seed_labor(session, orders, users)
    # After the orders: an ISSUE names the repair order that consumed the part, so
    # the ledger can only be written once there is something to name.
    ledger = await _seed_inventory_ledger(session, parts, orders, users)
    invoices = await _seed_invoices(
        session, customers, vehicles, users, orders, estimates
    )
    notifications = await _seed_notifications(
        session, users, customers, appointments, orders, invoices, parts
    )

    await session.flush()

    counts = {
        "suppliers": len(SUPPLIERS),
        "parts": len(PARTS),
        "customers": len(customers),
        "vehicles": len(vehicles),
        "service_requests": len(requests),
        "appointments": len(appointments),
        "inspections": len(inspections),
        "estimates": len(estimates),
        "repair_orders": len(orders),
        "inventory_transactions": len(ledger),
        "invoices": len(invoices),
        "notifications": len(notifications),
    }
    logger.info("Demo data seeded: %s", counts)
    return DemoDataResult(
        created=True,
        counts=counts,
        marker_customer_id=str(customers[0].id) if customers else None,
    )


async def _already_seeded(session: AsyncSession) -> bool:
    """Has this database been seeded already?

    Checked against the portal-linked customer, because that one has to exist by
    email: it is the only row whose identity the rest of the demo data depends
    on, and a duplicate of it would give the portal two accounts to choose from.
    """
    result = await session.execute(
        select(func.count(Customer.id)).where(Customer.email == DEMO_CUSTOMER_EMAIL)
    )
    return int(result.scalar_one() or 0) > 0


async def _users_by_email(session: AsyncSession) -> dict[str, User]:
    emails = [
        DEMO_OWNER_EMAIL,
        DEMO_ADVISOR_EMAIL,
        DEMO_TECH_EMAIL,
        DEMO_PARTS_EMAIL,
        DEMO_CUSTOMER_EMAIL,
    ]
    result = await session.execute(select(User).where(User.email.in_(emails)))
    return {u.email: u for u in result.scalars()}


async def _seed_customers(
    session: AsyncSession, users: dict[str, User]
) -> list[Customer]:
    created: list[Customer] = []
    for first, last, company, email, phone, linked, contact in CUSTOMERS:
        customer = Customer(
            first_name=first,
            last_name=last,
            company_name=company,
            email=email,
            phone=phone,
            preferred_contact=contact,
            customer_status=CustomerStatus.ACTIVE.value,
        )
        if linked:
            # The portal is driven by the customer behind the token, so exactly
            # one demo customer has a login attached.
            account = users.get(DEMO_CUSTOMER_EMAIL)
            if account is not None:
                customer.user_id = account.id
        session.add(customer)
        created.append(customer)
    await session.flush()
    return created


async def _seed_vehicles(
    session: AsyncSession, customers: list[Customer]
) -> list[Vehicle]:
    created: list[Vehicle] = []
    for idx, vin, plate, make, model, year, mileage, fuel, status, color in VEHICLES:
        customer = customers[idx]
        vehicle = Vehicle(
            customer_id=customer.id,
            vin=vin,
            license_plate=plate,
            make=make,
            model=model,
            year=year,
            color=color,
            mileage=mileage,
            fuel_type=fuel,
            status=status,
        )
        session.add(vehicle)
        created.append(vehicle)
    await session.flush()
    return created


async def _seed_catalog(session: AsyncSession) -> dict[str, Part]:
    """Suppliers first, then the parts they stock.

    Ordered deliberately: the catalog is what makes the inventory valuation report
    report anything, and a part with no cost makes that report return a number
    that is confidently wrong rather than obviously broken. Parts are not tied to
    a supplier here because this schema has no such column — the link lives on a
    purchase order, which is a document about buying rather than a property of the
    part.
    """
    suppliers: list[Supplier] = []
    for spec in SUPPLIERS:
        supplier = Supplier(**spec)
        session.add(supplier)
        suppliers.append(supplier)
    await session.flush()

    parts: dict[str, Part] = {}
    for index, (number, name, category, cost, price, on_hand, reorder, brand) in enumerate(
        PARTS
    ):
        part = Part(
            part_number=number,
            name=name,
            category=category,
            description=f"{brand} {name}, from {suppliers[index % len(suppliers)].name}",
            brand=brand,
            location=f"B{index % 3 + 1}-{index % 8 + 1}",
            unit_cost=cost,
            unit_price=price,
            quantity_on_hand=on_hand,
            reorder_level=reorder,
        )
        session.add(part)
        parts[number] = part
    await session.flush()
    return parts


async def _seed_service_requests(
    session: AsyncSession, customers: list[Customer], vehicles: list[Vehicle]
) -> list[ServiceRequest]:
    created: list[ServiceRequest] = []
    for idx, title, description, priority, status, vehicle_index in SERVICE_REQUESTS:
        request = ServiceRequest(
            customer_id=customers[idx].id,
            vehicle_id=vehicles[vehicle_index].id if vehicle_index is not None else None,
            title=title,
            description=description,
            priority=priority,
            status=status,
            service_advisor_notes="Logged at the counter." if status != "NEW" else None,
        )
        session.add(request)
        created.append(request)
    await session.flush()
    return created


async def _seed_appointments(
    session: AsyncSession,
    customers: list[Customer],
    vehicles: list[Vehicle],
    users: dict[str, User],
    requests: list[ServiceRequest],
    checkins: list[CheckIn],
) -> list[Appointment]:
    advisor = users.get(DEMO_ADVISOR_EMAIL)
    technician = users.get(DEMO_TECH_EMAIL)
    created: list[Appointment] = []
    for index, spec in enumerate(APPOINTMENTS):
        idx, vehicle_index, request_index, service_type, status, days, hour, concern = spec
        appointment = Appointment(
            customer_id=customers[idx].id,
            vehicle_id=vehicles[vehicle_index].id,
            service_request_id=(
                requests[request_index].id if request_index is not None else None
            ),
            # The two demo check-ins line up with the first two appointments, by
            # position: CHECK_INS names the appointment each one belongs to.
            checkin_id=checkins[index].id if index < len(checkins) else None,
            service_type=service_type,
            status=status,
            scheduled_start=_at(days, hour),
            duration_minutes=120,
            advisor_id=advisor.id if advisor else None,
            technician_id=technician.id if technician else None,
            bay=f"B{vehicle_index % 4 + 1}",
            customer_concern=concern,
        )
        session.add(appointment)
        created.append(appointment)
    await session.flush()
    return created


async def _seed_check_ins(
    session: AsyncSession,
    customers: list[Customer],
    vehicles: list[Vehicle],
    users: dict[str, User],
) -> list[CheckIn]:
    advisor = users.get(DEMO_ADVISOR_EMAIL)
    created: list[CheckIn] = []
    for idx, vehicle_index, _appointment_index, odometer, checkin_type, status, notes in CHECK_INS:
        checkin = CheckIn(
            customer_id=customers[idx].id,
            vehicle_id=vehicles[vehicle_index].id,
            checkin_type=checkin_type,
            status=status,
            odometer=odometer,
            service_advisor_id=advisor.id if advisor else None,
            notes=notes,
            # A promise in the customer's own words, not a timestamp: this is
            # what the advisor typed on the clipboard, and a formatted "4:00pm"
            # is what a real check-in screen holds.
            expected_completion="Around 4:00pm",
            tire_condition="Front tyres at 3mm, rears at 5mm.",
            fluid_levels="Topped up screen wash.",
            lights_status="All bulbs working.",
        )
        session.add(checkin)
        created.append(checkin)
    await session.flush()
    return created


async def _seed_inspections(
    session: AsyncSession,
    customers: list[Customer],
    vehicles: list[Vehicle],
    users: dict[str, User],
    checkins: list[CheckIn],
) -> list[Inspection]:
    technician = users.get(DEMO_TECH_EMAIL)
    created: list[Inspection] = []
    for index, (customer_index, vehicle_index, mileage) in enumerate(
        ((0, 0, 73980), (2, 3, 96480))
    ):
        inspection = Inspection(
            vehicle_id=vehicles[vehicle_index].id,
            customer_id=customers[customer_index].id,
            checkin_id=checkins[index].id if index < len(checkins) else None,
            technician_id=technician.id if technician else None,
            status=InspectionStatus.COMPLETED.value,
            mileage=mileage,
            overall_notes=(
                "Brake wear visible on the inner disc face."
                if index == 0
                else "Otherwise healthy; pads at 5mm."
            ),
        )
        session.add(inspection)
        created.append(inspection)
        # Flushed before the items: their foreign key is this id, and a batched
        # flush at the end would send both with a null parent.
        await session.flush()
        for category, item_name, item_status, measurement, recommendation, note in (
            (
                "BRAKES",
                "Front pad thickness",
                "RECOMMENDED",
                "3.0 mm",
                "REPLACE",
                "Below the 4mm service limit.",
            ),
            (
                "BRAKES",
                "Rear pad thickness",
                "ATTENTION",
                "5.5 mm",
                "MONITOR",
                "Usable for now.",
            ),
            ("TIRES", "Tread depth", "GOOD", "6 / 6 / 7 / 7 mm", "PASS", None),
            ("FLUIDS", "Engine oil level", "GOOD", "Between min and max", "PASS", None),
            (
                "LIGHTS",
                "Headlamp beams",
                "ATTENTION",
                "Right beam aimed low",
                "ADJUST",
                "Worth doing at the next visit.",
            ),
        ):
            session.add(
                InspectionItem(
                    inspection_id=inspection.id,
                    category=category,
                    item_name=item_name,
                    status=item_status,
                    measurement=measurement,
                    recommendation=recommendation,
                    notes=note,
                )
            )
    await session.flush()
    return created


async def _seed_estimates(
    session: AsyncSession,
    customers: list[Customer],
    vehicles: list[Vehicle],
    users: dict[str, User],
    requests: list[ServiceRequest],
    inspections: list[Inspection],
    parts: dict[str, Part],
) -> list[Estimate]:
    """Four estimates in four different states.

    One approved (which is what a repair order is built from), one sent and
    waiting on the customer (which is what the portal exists to show), and one
    draft (which is what the shop is looking at this morning). A dataset with
    only one of those teaches a demo nothing about the flow.

    Two of the sent ones: the second belongs to a customer with no login, so it is
    the case the notification service drops, and the fourth belongs to the portal
    customer, so it is the one that makes signing in as them worth doing.
    """
    advisor = users.get(DEMO_ADVISOR_EMAIL)
    created: list[Estimate] = []

    plans = [
        {
            "number": "EST-2026-0001",
            "customer": 0,
            "vehicle": 0,
            "request": 0,
            "inspection": 0,
            "status": EstimateStatus.APPROVED.value,
            "days": -20,
            "valid_days": 30,
            "notes": "Front brakes done, rear pads monitored.",
            "items": [
                ("LABOR", "Front brake replacement, 2.0h @ 145.00", 2.0, 145.00, None, None, "APPROVED"),
                ("PART", "Ceramic brake pad set, front", 1.0, 89.00, "BRK-PAD-1042", "Ceramic brake pad set, front", "APPROVED"),
                ("PART", "Vented brake rotor, front pair", 1.0, 149.00, "BRK-ROT-2210", "Vented brake rotor, front pair", "DECLINED"),
            ],
        },
        {
            "number": "EST-2026-0002",
            "customer": 2,
            "vehicle": 3,
            "request": 1,
            "inspection": 1,
            "status": EstimateStatus.SENT.value,
            "days": -2,
            "valid_days": 14,
            "notes": "Maintenance plus the wiper blade the customer mentioned.",
            "items": [
                ("LABOR", "60k service, 1.5h @ 145.00", 1.5, 145.00, None, None, "PENDING"),
                ("PART", "Synthetic 5W-30, 5 quarts", 1.0, 44.00, "OIL-5W30-5Q", "Synthetic 5W-30, 5 quarts", "PENDING"),
                ("PART", "Engine air filter", 1.0, 26.00, "FLT-AIR-3320", "Engine air filter", "PENDING"),
                ("PART", "Premium oil filter", 1.0, 18.50, "FLT-OIL-3310", "Premium oil filter", "PENDING"),
                ("PART", "Beam wiper blade, 22 inch", 1.0, 29.95, "WIP-BLADE-5501", "Beam wiper blade, 22 inch", "PENDING"),
                ("PART", "Cabin air filter", 1.0, 24.00, "FLT-CAB-3401", "Cabin air filter", "PENDING"),
            ],
        },
        {
            "number": "EST-2026-0003",
            "customer": 4,
            "vehicle": 5,
            "request": 2,
            "inspection": None,
            "status": EstimateStatus.DRAFT.value,
            "days": 0,
            "valid_days": 14,
            "notes": "Waiting on the diagnostic before this goes anywhere.",
            "items": [
                ("LABOR", "Diagnostic time, 1.0h @ 145.00", 1.0, 145.00, None, None, "PENDING"),
            ],
        },
        {
            # The portal customer's open question. Sent yesterday, nothing decided,
            # which is the exact state `open_only` exists to list — signing in as
            # customer@autofix.demo and finding an estimate with no decision on it
            # would leave the portal's main screen empty.
            "number": "EST-2026-0004",
            "customer": 0,
            "vehicle": 1,
            "request": 4,
            "inspection": None,
            "status": EstimateStatus.SENT.value,
            "days": -1,
            "valid_days": 14,
            "notes": "Sent after the phone report. High-voltage work quoted separately.",
            "items": [
                ("LABOR", "Battery cooling diagnostic, 1.5h @ 145.00", 1.5, 145.00, None, None, "PENDING"),
                ("PART", "Long-life coolant, 1 gallon", 1.0, 39.00, "COOL-COOL-2200", "Long-life coolant, 1 gallon", "PENDING"),
                ("FEE", "High-voltage safety isolation and re-energise", 1.0, 95.00, None, None, "PENDING"),
            ],
        },
    ]

    for plan in plans:
        estimate = Estimate(
            estimate_number=plan["number"],
            customer_id=customers[plan["customer"]].id,
            vehicle_id=vehicles[plan["vehicle"]].id,
            service_request_id=requests[plan["request"]].id,
            inspection_id=(
                inspections[plan["inspection"]].id
                if plan["inspection"] is not None
                else None
            ),
            created_by_id=advisor.id if advisor else None,
            status=plan["status"],
            tax_rate=TAX_RATE,
            valid_until=(date.today() + timedelta(days=plan["valid_days"])),
            notes=plan["notes"],
            sent_at=_at(plan["days"], 11) if plan["status"] != EstimateStatus.DRAFT.value else None,
            decided_at=(
                _at(plan["days"] + 1, 9)
                if plan["status"] == EstimateStatus.APPROVED.value
                else None
            ),
        )
        session.add(estimate)
        await session.flush()
        lines: list[EstimateItem] = []
        for sequence, spec in enumerate(plan["items"], start=1):
            item_type, description, hours, rate, part_number, part_name, status = spec
            item = EstimateItem(
                estimate_id=estimate.id,
                item_type=item_type,
                sequence=sequence,
                description=description,
                labor_hours=hours if item_type == EstimateItemType.LABOR.value else None,
                labor_rate=rate if item_type == EstimateItemType.LABOR.value else None,
                quantity=1.0 if item_type != EstimateItemType.LABOR.value else None,
                unit_price=rate if item_type != EstimateItemType.LABOR.value else None,
                part_number=part_number,
                part_name=part_name,
                status=status,
            )
            session.add(item)
            lines.append(item)
        await session.flush()
        _estimate_totals(estimate, lines)
        created.append(estimate)

    return created


async def _seed_repair_orders(
    session: AsyncSession,
    customers: list[Customer],
    vehicles: list[Vehicle],
    users: dict[str, User],
    appointments: list[Appointment],
    estimates: list[Estimate],
) -> list[RepairOrder]:
    """Seven orders spread across the shop and its history.

    A delivered one with completed tasks and labour (so the cycle-time and
    technician-productivity reports have something to measure), one on the lift,
    one draft nobody has started, an old completed job with a bill nobody has
    paid, a half-settled service, a small billed job on the portal customer's
    second car, and a settled repeat visit so retention has something to find.
    """
    advisor = users.get(DEMO_ADVISOR_EMAIL)
    technician = users.get(DEMO_TECH_EMAIL)
    approved, sent, draft = estimates[0], estimates[1], estimates[2]

    created: list[RepairOrder] = []

    delivered = RepairOrder(
        ro_number="RO-2026-0001",
        customer_id=approved.customer_id,
        vehicle_id=approved.vehicle_id,
        estimate_id=approved.id,
        appointment_id=appointments[0].id,
        advisor_id=advisor.id if advisor else None,
        technician_id=technician.id if technician else None,
        status=RepairOrderStatus.DELIVERED.value,
        odometer_in=73980,
        odometer_out=73980,
        bay="B1",
        promised_at=_at(-20, 16),
        started_at=_at(-20, 10),
        completed_at=_at(-20, 13),
        delivered_at=_at(-18, 17),
        notes="Customer waited; same-day pickup agreed.",
        customer_notes="Called to confirm the pads were done, not just the rotors.",
    )
    session.add(delivered)
    await session.flush()
    for sequence, (description, status, _hours) in enumerate(
        (
            ("Strip front calipers and swap pads", RepairTaskStatus.COMPLETED.value, 1.0),
            ("Skim and bleed front caliper", RepairTaskStatus.COMPLETED.value, 0.5),
            ("Road test and re-torque", RepairTaskStatus.COMPLETED.value, 0.5),
            ("Machine the front rotors", RepairTaskStatus.SKIPPED.value, None),
        ),
        start=1,
    ):
        session.add(
            RepairTask(
                repair_order_id=delivered.id,
                description=description,
                status=status,
                sequence=sequence,
                assigned_to_id=technician.id if technician else None,
                completed_at=(
                    _at(-20, 12) if status == RepairTaskStatus.COMPLETED.value else None
                ),
            )
        )
    created.append(delivered)

    in_progress = RepairOrder(
        ro_number="RO-2026-0002",
        customer_id=sent.customer_id,
        vehicle_id=sent.vehicle_id,
        estimate_id=sent.id,
        appointment_id=appointments[1].id,
        advisor_id=advisor.id if advisor else None,
        technician_id=technician.id if technician else None,
        status=RepairOrderStatus.IN_PROGRESS.value,
        odometer_in=96480,
        bay="B3",
        promised_at=_at(-8, 16),
        started_at=_at(-8, 9),
        notes="Awaiting the customer's decision on the full service.",
    )
    session.add(in_progress)
    await session.flush()
    for sequence, (description, status) in enumerate(
        (
            ("Oil and filter change", RepairTaskStatus.COMPLETED.value),
            ("Fluid top-up and tyre pressures", RepairTaskStatus.IN_PROGRESS.value),
            ("Replace wiper blades", RepairTaskStatus.PENDING.value),
        ),
        start=1,
    ):
        session.add(
            RepairTask(
                repair_order_id=in_progress.id,
                description=description,
                status=status,
                sequence=sequence,
                assigned_to_id=technician.id if technician else None,
                completed_at=(
                    _at(-8, 10) if status == RepairTaskStatus.COMPLETED.value else None
                ),
            )
        )
    created.append(in_progress)

    not_started = RepairOrder(
        ro_number="RO-2026-0003",
        customer_id=draft.customer_id,
        vehicle_id=draft.vehicle_id,
        advisor_id=advisor.id if advisor else None,
        status=RepairOrderStatus.DRAFT.value,
        odometer_in=88415,
        bay="B2",
        notes="Raised so the diagnostic can be billed; nothing signed off yet.",
    )
    session.add(not_started)
    await session.flush()
    session.add(
        RepairTask(
            repair_order_id=not_started.id,
            description="Read codes and diagnose the check engine light",
            status=RepairTaskStatus.PENDING.value,
            sequence=1,
        )
    )
    created.append(not_started)

    # An old, completed order that has not been paid for. The invoice that is
    # overdue is *this* work: an overdue bill attached to somebody else's
    # delivered repair would be a demo dataset nobody believes, and the chase
    # list is the most realistic thing a garage has.
    stale = RepairOrder(
        ro_number="RO-2025-0142",
        customer_id=customers[5].id,
        vehicle_id=vehicles[6].id,
        advisor_id=advisor.id if advisor else None,
        technician_id=technician.id if technician else None,
        status=RepairOrderStatus.COMPLETED.value,
        odometer_in=152100,
        odometer_out=152100,
        bay="B4",
        promised_at=_at(-97, 16),
        started_at=_at(-97, 9),
        completed_at=_at(-96, 15),
        notes="Battery and brakes. Left with the customer, unpaid.",
        customer_notes="Said they would call about the invoice the following week.",
    )
    session.add(stale)
    await session.flush()
    for sequence, (description, status) in enumerate(
        (
            ("Replace battery and dispose of the old unit", RepairTaskStatus.COMPLETED.value),
            ("Front brake service, pads and rotors", RepairTaskStatus.COMPLETED.value),
            ("Load test the charging system", RepairTaskStatus.COMPLETED.value),
        ),
        start=1,
    ):
        session.add(
            RepairTask(
                repair_order_id=stale.id,
                description=description,
                status=status,
                sequence=sequence,
                assigned_to_id=technician.id if technician else None,
                completed_at=(
                    _at(-96, 14) if status == RepairTaskStatus.COMPLETED.value else None
                ),
            )
        )
    created.append(stale)

    # A second completed order, half settled. One repair order raises exactly one
    # invoice — the schema enforces it — so a second bill needs a second order,
    # and pretending otherwise would seed data the database forbids.
    half_settled = RepairOrder(
        ro_number="RO-2026-0004",
        customer_id=customers[1].id,
        vehicle_id=vehicles[2].id,
        advisor_id=advisor.id if advisor else None,
        technician_id=technician.id if technician else None,
        status=RepairOrderStatus.DELIVERED.value,
        odometer_in=118600,
        odometer_out=118600,
        bay="B2",
        promised_at=_at(-9, 16),
        started_at=_at(-9, 9),
        completed_at=_at(-9, 12),
        delivered_at=_at(-9, 16),
        notes="Service and a rear adjustment. Customer paid half on the day.",
        customer_notes="Asked to be reminded about the balance.",
    )
    session.add(half_settled)
    await session.flush()
    for sequence, (description, status) in enumerate(
        (
            ("Oil and filter change", RepairTaskStatus.COMPLETED.value),
            ("Rear brake adjustment", RepairTaskStatus.COMPLETED.value),
            ("Check brake pad wear", RepairTaskStatus.COMPLETED.value),
        ),
        start=1,
    ):
        session.add(
            RepairTask(
                repair_order_id=half_settled.id,
                description=description,
                status=status,
                sequence=sequence,
                assigned_to_id=technician.id if technician else None,
                completed_at=(
                    _at(-9, 12) if status == RepairTaskStatus.COMPLETED.value else None
                ),
            )
        )
    created.append(half_settled)

    # A small job on the portal customer's other car, billed and not yet paid.
    # Without it the demo account has one invoice and it is settled, so signing in
    # as them shows no outstanding balance and the chase list on their account is
    # empty. No estimate is attached: small jobs skip the estimate, and inventing
    # one would contradict the rule that an order is only raised against approved
    # work — this work was agreed at the counter.
    small_job = RepairOrder(
        ro_number="RO-2026-0005",
        customer_id=customers[0].id,
        vehicle_id=vehicles[1].id,
        advisor_id=advisor.id if advisor else None,
        technician_id=technician.id if technician else None,
        status=RepairOrderStatus.DELIVERED.value,
        odometer_in=38640,
        odometer_out=38702,
        bay="B1",
        promised_at=_at(-4, 16),
        started_at=_at(-4, 10),
        completed_at=_at(-4, 12),
        delivered_at=_at(-4, 16),
        notes="Agreed at the counter: filters and wipers while it was in.",
    )
    session.add(small_job)
    await session.flush()
    for sequence, (description, status) in enumerate(
        (
            ("Replace cabin air filter", RepairTaskStatus.COMPLETED.value),
            ("Replace wiper blades", RepairTaskStatus.COMPLETED.value),
        ),
        start=1,
    ):
        session.add(
            RepairTask(
                repair_order_id=small_job.id,
                description=description,
                status=status,
                sequence=sequence,
                assigned_to_id=technician.id if technician else None,
                completed_at=_at(-4, 11),
            )
        )
    created.append(small_job)

    # The customer's second visit, settled. Retention is measured on settled bills,
    # and a demo shop where exactly one person has ever paid a bill reports a zero
    # repeat rate — which is precisely the "healthy shop full of one-timers" the
    # report is built to contradict.
    repeat_visit = RepairOrder(
        ro_number="RO-2026-0006",
        customer_id=customers[1].id,
        vehicle_id=vehicles[2].id,
        advisor_id=advisor.id if advisor else None,
        technician_id=technician.id if technician else None,
        status=RepairOrderStatus.DELIVERED.value,
        odometer_in=114900,
        odometer_out=114940,
        bay="B3",
        promised_at=_at(-26, 16),
        started_at=_at(-26, 9),
        completed_at=_at(-26, 11),
        delivered_at=_at(-26, 16),
        notes="Annual service. Same car as last winter.",
        customer_notes="Booked straight away this time.",
    )
    session.add(repeat_visit)
    await session.flush()
    for sequence, (description, status) in enumerate(
        (
            ("Oil and filter change", RepairTaskStatus.COMPLETED.value),
            ("Air filter replaced", RepairTaskStatus.COMPLETED.value),
            ("Check brake pad wear", RepairTaskStatus.COMPLETED.value),
        ),
        start=1,
    ):
        session.add(
            RepairTask(
                repair_order_id=repeat_visit.id,
                description=description,
                status=status,
                sequence=sequence,
                assigned_to_id=technician.id if technician else None,
                completed_at=_at(-26, 10),
            )
        )
    created.append(repeat_visit)

    # The same customer a year earlier, which is what makes the retention report
    # say something. "Retained" means settled more than one bill *ever*, and the
    # case it exists to find is the customer who came back after a long gap — so
    # the earlier visit has to be a paid bill well outside any 30-day window.
    last_winter = RepairOrder(
        ro_number="RO-2025-0318",
        customer_id=customers[1].id,
        vehicle_id=vehicles[2].id,
        advisor_id=advisor.id if advisor else None,
        technician_id=technician.id if technician else None,
        status=RepairOrderStatus.DELIVERED.value,
        odometer_in=98400,
        odometer_out=98400,
        bay="B3",
        promised_at=_at(-240, 16),
        started_at=_at(-240, 9),
        completed_at=_at(-240, 13),
        delivered_at=_at(-240, 16),
        notes="Winter service on the same car.",
    )
    session.add(last_winter)
    await session.flush()
    for sequence, (description, status) in enumerate(
        (
            ("Winter service, oil and filters", RepairTaskStatus.COMPLETED.value),
            ("Battery load test", RepairTaskStatus.COMPLETED.value),
        ),
        start=1,
    ):
        session.add(
            RepairTask(
                repair_order_id=last_winter.id,
                description=description,
                status=status,
                sequence=sequence,
                assigned_to_id=technician.id if technician else None,
                completed_at=_at(-240, 12),
            )
        )
    created.append(last_winter)

    return created


async def _seed_inventory_ledger(
    session: AsyncSession,
    parts: dict[str, Part],
    orders: list[RepairOrder],
    users: dict[str, User],
) -> list[InventoryTransaction]:
    """Give every part on the shelf a history that adds up to its balance.

    The inventory module is emphatic that ``quantity_on_hand`` is a running total
    and never a field anybody edits — every unit is on the ledger, "even the
    opening balance". A seed that set the balance directly would contradict the
    rule the rest of the module is built on, and a demo database whose stock has
    no transactions behind it cannot answer the one question the ledger exists
    for: where did this come from.

    So each part opens with a ``RECEIPT`` from nothing to its opening figure, and
    the parts that were fitted to a repair order are then ``ISSUE``d off it. The
    opening figure is the closing balance from :data:`PARTS` *plus* whatever was
    issued, which is why the numbers still read as the stock on the shelf while
    the arithmetic behind them is a real ledger.
    """
    parts_staff = users.get(DEMO_PARTS_EMAIL)
    technician = users.get(DEMO_TECH_EMAIL)

    issued: dict[str, list[tuple[float, RepairOrder, str]]] = {}
    for part_number, order_index, quantity, reason in ISSUES:
        issued.setdefault(part_number, []).append((quantity, orders[order_index], reason))

    created: list[InventoryTransaction] = []
    for number, _, _, unit_cost, _, on_hand, _, _ in PARTS:
        movements = issued.get(number, [])
        # The closing balance in PARTS plus what was issued, so the shelf reads as
        # specified while the arithmetic behind it is a ledger rather than a number
        # somebody typed.
        opening = float(on_hand) + sum(q for q, _, _ in movements)
        receipt = InventoryTransaction(
            part_id=parts[number].id,
            transaction_type=InventoryTransactionType.RECEIPT.value,
            quantity=opening,
            quantity_before=0.0,
            quantity_after=opening,
            unit_cost=unit_cost,
            reference="Opening stock",
            performed_by_id=parts_staff.id if parts_staff else None,
            reason="Opening balance taken at the start of the trading year.",
        )
        session.add(receipt)
        created.append(receipt)
        await session.flush()

        balance = opening
        for quantity, order, reason in movements:
            after = balance - quantity
            issue = InventoryTransaction(
                part_id=parts[number].id,
                transaction_type=InventoryTransactionType.ISSUE.value,
                quantity=-quantity,
                quantity_before=balance,
                quantity_after=after,
                unit_cost=unit_cost,
                repair_order_id=order.id,
                reference=order.ro_number,
                performed_by_id=technician.id if technician else None,
                reason=reason,
            )
            session.add(issue)
            created.append(issue)
            balance = after
        if movements:
            await session.flush()

    return created


async def _seed_notifications(
    session: AsyncSession,
    users: dict[str, User],
    customers: list[Customer],
    appointments: list[Appointment],
    orders: list[RepairOrder],
    invoices: list[Invoice],
    parts: dict[str, Part],
) -> list[Notification]:
    """Leave something in the attention centres, so Phase 17 is visible.

    Notifications are normally written by the event system when a service acts. A
    demo database seeded only through the models would therefore have an empty
    notification list for every user, and the badge would read zero — which looks
    broken rather than empty.

    These are the notices the seeded history actually implies: the customer who was
    billed and the appointment they are coming to, the technician with a car on the
    lift, the advisor chasing an overdue bill, and the parts clerk whose stock has
    fallen to its reorder point. Each is addressed to whoever the record is about,
    which for a customer means resolving their login — the same indirection the
    delivery service performs, and the same rule that an event with neither a login
    nor a customer is refused rather than guessed at.
    """
    advisor = users.get(DEMO_ADVISOR_EMAIL)
    technician = users.get(DEMO_TECH_EMAIL)
    parts_staff = users.get(DEMO_PARTS_EMAIL)

    plans: list[NotificationEvent] = []

    # Every invoice that was actually sent tells its customer so — the same rule
    # the invoice service applies when it issues one. Applied from the data rather
    # than written out notice by notice, so the demo cannot fall out of step with
    # the product: a bill that is issued without a notice, or a notice for a draft,
    # is the sort of thing that only shows up when somebody reads the data.
    for invoice in invoices:
        if invoice.issued_at is None:
            continue
        plans.append(
            invoice_issued_event(
                customer_id=invoice.customer_id,
                invoice_number=invoice.invoice_number,
                invoice_id=invoice.id,
            )
        )

    # The appointment requests that are still open. A submitted request is
    # acknowledged, which is also the demo customer's unread notice: the portal
    # account has a live conversation with the shop rather than a closed one.
    for appointment in appointments:
        if appointment.status != AppointmentStatus.REQUESTED.value:
            continue
        plans.append(
            NotificationEvent(
                notification_type=NotificationType.APPOINTMENT_UPDATE.value,
                title="We have your appointment request",
                body=(
                    f"Requested for {appointment.scheduled_start:%A %d %B at %H:%M}. "
                    "The shop will confirm shortly."
                ),
                customer_id=appointment.customer_id,
                entity_type="appointment",
                entity_id=appointment.id,
                dedupe_key=f"appointment_requested:{appointment.id}",
            )
        )

    # The car on the lift, so the technician's dashboard has a live job on it.
    on_the_lift = next(
        (o for o in orders if o.status == RepairOrderStatus.IN_PROGRESS.value), None
    )
    if technician is not None and on_the_lift is not None:
        plans.append(
            NotificationEvent(
                notification_type=NotificationType.REPAIR_ORDER_UPDATE.value,
                title=f"{on_the_lift.ro_number} is on the lift",
                body="Oil and filter change is booked against this order.",
                recipient_id=technician.id,
                entity_type="repair_order",
                entity_id=on_the_lift.id,
                dedupe_key=f"repair_order_started:{on_the_lift.id}",
            )
        )

    # The chase list: the oldest unpaid bill. Picked as the one past its due date,
    # which is the definition the reports use, rather than by list position.
    today = date.today()
    overdue = next(
        (
            i
            for i in invoices
            if i.status == InvoiceStatus.ISSUED.value
            and i.amount_paid == 0
            and i.due_date is not None
            and i.due_date < today
        ),
        None,
    )
    if advisor is not None and overdue is not None:
        plans.append(
            NotificationEvent(
                notification_type=NotificationType.INVOICE_OVERDUE.value,
                title=f"Invoice {overdue.invoice_number} is overdue",
                body=(
                    "Issued and unpaid past its due date. Worth a call before the "
                    "next visit."
                ),
                recipient_id=advisor.id,
                entity_type="invoice",
                entity_id=overdue.id,
                priority=PRIORITY_HIGH,
                dedupe_key=f"invoice_overdue:{overdue.id}",
            )
        )

    # Stock at or below its reorder point, one notice per part — which is what the
    # dedupe key buys: a nightly low-stock sweep must not stack them.
    if parts_staff is not None:
        for number, name, _, _, _, on_hand, reorder_level, _ in PARTS:
            part = parts[number]
            if on_hand > reorder_level:
                continue
            plans.append(
                NotificationEvent(
                    notification_type=NotificationType.INVENTORY_LOW.value,
                    title=f"{name} is low on stock",
                    body=(
                        f"{part.quantity_on_hand} left, at or below the reorder "
                        f"point of {part.reorder_level}."
                    ),
                    recipient_id=parts_staff.id,
                    entity_type="part",
                    entity_id=part.id,
                    priority=PRIORITY_HIGH,
                    dedupe_key=f"inventory_low:{part.id}",
                )
            )

    logins = {c.id: c.user_id for c in customers if c.user_id is not None}
    # A bill they have already settled has plainly been opened; everything still
    # waiting on somebody is unread, which is what puts a number on the badge in
    # the demo.
    settled_ids = {i.id for i in invoices if i.status == InvoiceStatus.PAID.value}
    created: list[Notification] = []
    for event in plans:
        addressed = event.normalised()
        recipient = addressed.recipient_id or logins.get(addressed.customer_id)
        if recipient is None:
            # Nobody to address it to: dropped quietly rather than failing the
            # seed, which is how the delivery service treats a customer with no
            # account. Four of the six demo customers are in exactly that state.
            continue
        # A bill they have already settled has plainly been opened; everything
        # still waiting on somebody is unread, which is what puts a number on the
        # badge in the demo.
        settled = addressed.entity_id in settled_ids
        notification = Notification(
            recipient_id=recipient,
            notification_type=addressed.notification_type,
            title=addressed.title,
            body=addressed.body,
            entity_type=addressed.entity_type,
            entity_id=addressed.entity_id,
            priority=addressed.priority,
            dedupe_key=addressed.dedupe_key,
            read_at=_at(-17, 19) if settled else None,
        )
        session.add(notification)
        created.append(notification)

    await session.flush()
    return created


async def _seed_labor(
    session: AsyncSession, orders: list[RepairOrder], users: dict[str, User]
) -> None:
    """Billable hours, on the work that actually happened.

    Driven off completed tasks rather than invented numbers, so the hours on an
    order always agree with what its task list says was done — a report that
    showed 14 hours on an order with one completed task would be a bug in the
    data, not a bug in the report.
    """
    technician = users.get(DEMO_TECH_EMAIL)
    for order in orders:
        # Flushed before the query on purpose: sessions here do not autoflush, and
        # the tasks added for the last order would otherwise be invisible to the
        # select that is supposed to be counting them.
        await session.flush()
        result = await session.execute(
            select(RepairTask).where(RepairTask.repair_order_id == order.id)
        )
        for task in result.scalars():
            if task.status != RepairTaskStatus.COMPLETED.value:
                continue
            hours = 1.0 if "brake" in task.description.lower() else 0.5
            session.add(
                LaborRecord(
                    repair_order_id=order.id,
                    repair_task_id=task.id,
                    technician_id=technician.id if technician else None,
                    description=task.description,
                    actual_hours=hours,
                    billable_hours=hours,
                    hourly_rate=145.00,
                    performed_at=order.completed_at or _at(0, 15),
                    notes="Booked to the standard technician rate.",
                )
            )
    await session.flush()


async def _seed_invoices(
    session: AsyncSession,
    customers: list[Customer],
    vehicles: list[Vehicle],
    users: dict[str, User],
    orders: list[RepairOrder],
    estimates: list[Estimate],
) -> list[Invoice]:
    """Six invoices covering every state the money can be in.

    Two settled, one part-paid, one issued-and-overdue, one issued-and-not-yet-due,
    and a draft. The revenue report's whole point is the difference between what
    was charged and what was collected, and a seed where every bill is settled
    makes that report lie; the not-yet-due one is what keeps the receivables list
    from being just the chase list, and the second settled bill is what gives the
    retention report a returning customer to find.

    Returned in the order they are written, because the notifications below address
    them by property — issued, or past its due date — rather than by position.
    """
    owner = users.get(DEMO_OWNER_EMAIL)
    delivered, in_progress, _draft_order, stale, half_settled, small_job, repeat_visit, last_winter = orders
    approved = estimates[0]
    written: list[Invoice] = []

    # 1. Settled in full by card, six days after the work was delivered.
    paid_items = [
        ("LABOR", "Front brake replacement, 2.0h @ 145.00", 1.0, 290.00, None, None),
        ("PART", "Ceramic brake pad set, front", 1.0, 89.00, "BRK-PAD-1042", "Ceramic brake pad set, front"),
        ("FEE", "Shop supplies and waste disposal", 1.0, 15.00, None, None),
    ]
    paid = await _make_invoice(
        session,
        number="INV-2026-0001",
        customer=customers[0],
        vehicle=vehicles[0],
        order=delivered,
        estimate=approved,
        created_by=owner,
        status=InvoiceStatus.PAID.value,
        invoice_date=_at(-18).date(),
        due_date=(_at(-18).date() + timedelta(days=30)),
        items=paid_items,
        issued_days=-18,
        paid_days=-12,
    )
    written.append(paid)
    session.add(
        Payment(
            invoice_id=paid.id,
            amount=paid.total,
            method="CARD",
            status=PaymentStatus.RECORDED.value,
            payment_date=_at(-12).date(),
            reference="AUTH 4471",
            recorded_by_id=owner.id if owner else None,
            notes="Paid in full at the front desk.",
        )
    )

    # 2. Half paid, because the customer came back for the rear pads later.
    part_items = [
        ("LABOR", "Rear brake adjustment, 1.0h @ 145.00", 1.0, 145.00, None, None),
        ("PART", "Premium oil filter", 1.0, 18.50, "FLT-OIL-3310", "Premium oil filter"),
        ("PART", "Synthetic 5W-30, 5 quarts", 1.0, 44.00, "OIL-5W30-5Q", "Synthetic 5W-30, 5 quarts"),
    ]
    part_paid = await _make_invoice(
        session,
        number="INV-2026-0002",
        customer=customers[1],
        vehicle=vehicles[2],
        order=half_settled,
        estimate=None,
        created_by=owner,
        status=InvoiceStatus.PARTIALLY_PAID.value,
        invoice_date=_at(-9).date(),
        due_date=(_at(-9).date() + timedelta(days=30)),
        items=part_items,
        issued_days=-9,
    )
    written.append(part_paid)
    first_payment = round_money(part_paid.total / 2)
    session.add(
        Payment(
            invoice_id=part_paid.id,
            amount=first_payment,
            method="CASH",
            status=PaymentStatus.RECORDED.value,
            payment_date=_at(-4).date(),
            reference="Till 2",
            recorded_by_id=owner.id if owner else None,
            notes="Deposit on account.",
        )
    )
    part_paid.amount_paid = first_payment
    await session.flush()

    # 3. Issued three months ago and never touched: this is the chase list.
    overdue_items = [
        ("LABOR", "Full brake service, 3.0h @ 145.00", 1.0, 435.00, None, None),
        ("PART", "Vented brake rotor, front pair", 1.0, 149.00, "BRK-ROT-2210", "Vented brake rotor, front pair"),
        ("PART", "AGM battery group 48", 1.0, 329.00, "BAT-AGM-7700", "AGM battery group 48"),
    ]
    overdue = await _make_invoice(
        session,
        number="INV-2025-0098",
        customer=customers[5],
        vehicle=vehicles[6],
        order=stale,
        estimate=None,
        created_by=owner,
        status=InvoiceStatus.ISSUED.value,
        invoice_date=_at(-95).date(),
        due_date=(_at(-95).date() + timedelta(days=30)),
        items=overdue_items,
        issued_days=-95,
    )
    overdue.amount_paid = 0.0
    written.append(overdue)

    # 4. A draft sitting on the desk: raised, not sent, not owed.
    draft = await _make_invoice(
        session,
        number="INV-2026-0003",
        customer=customers[4],
        vehicle=vehicles[5],
        order=in_progress,
        estimate=estimates[1],
        created_by=owner,
        status=InvoiceStatus.DRAFT.value,
        invoice_date=date.today(),
        due_date=(date.today() + timedelta(days=30)),
        items=[
            ("LABOR", "Diagnostic time, 1.0h @ 145.00", 1.0, 145.00, None, None),
        ],
        issued_days=None,
    )
    draft.amount_paid = 0.0
    written.append(draft)

    # 5. Issued four days ago and not yet due: money owed, not money chased. The
    # portal customer's, so their account shows a live balance.
    awaiting = await _make_invoice(
        session,
        number="INV-2026-0004",
        customer=customers[0],
        vehicle=vehicles[1],
        order=small_job,
        estimate=None,
        created_by=owner,
        status=InvoiceStatus.ISSUED.value,
        invoice_date=_at(-4).date(),
        due_date=(_at(-4).date() + timedelta(days=30)),
        items=[
            ("LABOR", "Filters and wipers, 1.0h @ 145.00", 1.0, 145.00, None, None),
            ("PART", "Cabin air filter", 1.0, 24.00, "FLT-CAB-3401", "Cabin air filter"),
            ("PART", "Beam wiper blade, 22 inch", 2.0, 29.95, "WIP-BLADE-5501", "Beam wiper blade, 22 inch"),
        ],
        issued_days=-4,
    )
    awaiting.amount_paid = 0.0
    written.append(awaiting)

    # 6. Settled a month ago, and the same customer also has the part-paid bill
    # above: two settled bills is what makes them a *retained* customer.
    repeat_items = [
        ("LABOR", "Annual service, 2.0h @ 145.00", 1.0, 290.00, None, None),
        ("PART", "Premium oil filter", 1.0, 18.50, "FLT-OIL-3310", "Premium oil filter"),
        ("PART", "Engine air filter", 1.0, 26.00, "FLT-AIR-3320", "Engine air filter"),
        ("PART", "Synthetic 5W-30, 5 quarts", 1.0, 44.00, "OIL-5W30-5Q", "Synthetic 5W-30, 5 quarts"),
    ]
    repeat = await _make_invoice(
        session,
        number="INV-2026-0000",
        customer=customers[1],
        vehicle=vehicles[2],
        order=repeat_visit,
        estimate=None,
        created_by=owner,
        status=InvoiceStatus.PAID.value,
        invoice_date=_at(-26).date(),
        due_date=(_at(-26).date() + timedelta(days=30)),
        items=repeat_items,
        issued_days=-26,
        paid_days=-24,
    )
    written.append(repeat)
    session.add(
        Payment(
            invoice_id=repeat.id,
            amount=repeat.total,
            method="BANK_TRANSFER",
            status=PaymentStatus.RECORDED.value,
            payment_date=_at(-24).date(),
            reference="FT-88213/Sept",
            recorded_by_id=owner.id if owner else None,
            notes="Bank transfer, cleared in two days.",
        )
    )

    # 7. Last winter, also settled: the earlier of the two bills that make this
    # customer a returning one.
    older = await _make_invoice(
        session,
        number="INV-2025-0321",
        customer=customers[1],
        vehicle=vehicles[2],
        order=last_winter,
        estimate=None,
        created_by=owner,
        status=InvoiceStatus.PAID.value,
        invoice_date=_at(-240).date(),
        due_date=(_at(-240).date() + timedelta(days=30)),
        items=[
            ("LABOR", "Winter service, 2.0h @ 145.00", 1.0, 290.00, None, None),
            ("PART", "Synthetic 5W-30, 5 quarts", 1.0, 44.00, "OIL-5W30-5Q", "Synthetic 5W-30, 5 quarts"),
            ("PART", "Premium oil filter", 1.0, 18.50, "FLT-OIL-3310", "Premium oil filter"),
        ],
        issued_days=-240,
        paid_days=-238,
    )
    written.append(older)
    session.add(
        Payment(
            invoice_id=older.id,
            amount=older.total,
            method="CARD",
            status=PaymentStatus.RECORDED.value,
            payment_date=_at(-238).date(),
            reference="AUTH 3390",
            recorded_by_id=owner.id if owner else None,
            notes="Paid on collection.",
        )
    )

    await session.flush()
    return written


async def _make_invoice(
    session: AsyncSession,
    *,
    number: str,
    customer: Customer,
    vehicle: Vehicle,
    order: RepairOrder,
    estimate: Estimate | None,
    created_by: User | None,
    status: str,
    invoice_date,
    due_date,
    items: list[tuple[str, str, float, float, str | None, str | None]],
    issued_days: int | None,
    paid_days: int | None = None,
) -> Invoice:
    """Write one invoice and its lines, with the totals actually adding up."""
    invoice = Invoice(
        invoice_number=number,
        customer_id=customer.id,
        vehicle_id=vehicle.id,
        repair_order_id=order.id,
        estimate_id=estimate.id if estimate else None,
        status=status,
        invoice_date=invoice_date,
        due_date=due_date,
        tax_rate=TAX_RATE,
        amount_paid=0.0,
        created_by_id=created_by.id if created_by else None,
        issued_at=_at(issued_days, 12) if issued_days is not None else None,
        paid_at=_at(paid_days, 12) if paid_days is not None else None,
        notes=None,
    )
    session.add(invoice)
    await session.flush()

    lines: list[InvoiceItem] = []
    for sequence, (item_type, description, quantity, unit_price, part_number, part_name) in enumerate(
        items, start=1
    ):
        line = InvoiceItem(
            invoice_id=invoice.id,
            item_type=item_type,
            source="ESTIMATE" if item_type == "LABOR" and estimate else "MANUAL",
            sequence=sequence,
            description=description,
            quantity=quantity,
            unit_price=unit_price,
            part_number=part_number,
            part_name=part_name,
        )
        session.add(line)
        lines.append(line)
    await session.flush()

    _invoice_totals(invoice, lines)
    if paid_days is not None:
        invoice.amount_paid = invoice.total
    return invoice


__all__ = [
    "DEMO_ADVISOR_EMAIL",
    "DEMO_CUSTOMER_EMAIL",
    "DEMO_OWNER_EMAIL",
    "DEMO_PARTS_EMAIL",
    "DEMO_TECH_EMAIL",
    "DemoDataResult",
    "seed_demo_data",
]
