"""Invoice data models.

An :class:`Invoice` is the shop's bill for work that has been done: which
vehicle, which customer, which repair order it settles, and what it charges.
:class:`InvoiceItem` is one line on that bill.

The estimate stays the source of truth for *what was agreed*; the invoice is the
document that asks for the money. A line copied from the estimate is a snapshot
of the customer's approval — frozen onto the invoice so that re-pricing the
catalog, editing the estimate, or renaming a part later cannot quietly rewrite
what the customer was sent. Lines the shop added itself (extra work found
during the job) are marked ``MANUAL`` and may still be corrected while the
invoice is a draft.

Statuses
--------
``DRAFT``         written but not yet sent; the only editable state
``ISSUED``        sent to the customer, awaiting payment
``PARTIALLY_PAID`` some money received, a balance remains
``PAID``          settled in full — terminal
``VOID``          written off; nothing is owed — terminal

``OVERDUE`` is deliberately *not* a status. An invoice is overdue when its due
date has passed and it is still not paid, which is a fact about the clock and
the balance rather than a decision anybody made; storing it would need a
scheduled job to keep it true.

Payment state is never set by hand. The invoice holds ``amount_paid`` and the
statuses above are derived from it by :mod:`app.payments`, so an invoice can
only be marked paid by money actually being recorded against it.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.dates import today
from app.core.database import TimestampedBase

CENTS = 2

# The shop's default terms. A bill with no agreed date is still owed on a
# schedule, and "net 30" is what a garage invoice means when nobody wrote
# anything else down.
DEFAULT_PAYMENT_TERM_DAYS = 30


def round_money(value: float) -> float:
    """Round a monetary amount to cents.

    Nudged off the exact half-cent first, so ``0.125`` becomes ``0.13`` rather
    than a parity-dependent result.
    """
    return round(value + 1e-9, CENTS)


class InvoiceStatus(str, enum.Enum):
    """Lifecycle of an invoice."""

    DRAFT = "DRAFT"
    ISSUED = "ISSUED"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    VOID = "VOID"


class InvoiceItemType(str, enum.Enum):
    """Kind of charge an invoice line represents."""

    LABOR = "LABOR"
    PART = "PART"
    SERVICE = "SERVICE"
    FEE = "FEE"
    DISCOUNT = "DISCOUNT"


class InvoiceItemSource(str, enum.Enum):
    """Where an invoice line came from."""

    # Copied from a line the customer approved on the estimate.
    ESTIMATE = "ESTIMATE"
    # Added by the shop, e.g. extra work agreed at the counter.
    MANUAL = "MANUAL"


INVOICE_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in InvoiceStatus)
INVOICE_ITEM_TYPE_VALUES: tuple[str, ...] = tuple(s.value for s in InvoiceItemType)
INVOICE_ITEM_SOURCE_VALUES: tuple[str, ...] = tuple(s.value for s in InvoiceItemSource)

# Statuses that will accept no further money and can never be reopened. VOID is
# the only one: a PAID invoice can go back to owing money, but only because a
# payment against it was reversed — that is a fact about the money, not an edit to
# the document the customer is holding.
TERMINAL_INVOICE_STATUSES: frozenset[str] = frozenset({InvoiceStatus.VOID.value})

# Statuses in which a customer still owes money.
PAYABLE_INVOICE_STATUSES: frozenset[str] = frozenset(
    {
        InvoiceStatus.ISSUED.value,
        InvoiceStatus.PARTIALLY_PAID.value,
    }
)

# Repair-order statuses a bill may be raised against. Work is only billed after
# it has been checked, so a defect found by QC is corrected before the customer
# is asked to pay for it.
BILLABLE_RO_STATUSES: frozenset[str] = frozenset(
    {
        "QC_PASSED",
        "DELIVERED",
    }
)

INVOICE_STATUS_TRANSITIONS: dict[str, list[str]] = {
    InvoiceStatus.DRAFT.value: [
        InvoiceStatus.ISSUED.value,
    ],
    # Money arriving moves an issued invoice through the payment states. The
    # backwards steps are reachable only by reversing a payment, never by a
    # caller naming a status: money that turns out to have been taken in error
    # has to put the balance back.
    InvoiceStatus.ISSUED.value: [
        InvoiceStatus.PARTIALLY_PAID.value,
        InvoiceStatus.PAID.value,
        InvoiceStatus.VOID.value,
    ],
    InvoiceStatus.PARTIALLY_PAID.value: [
        InvoiceStatus.PARTIALLY_PAID.value,
        InvoiceStatus.PAID.value,
        InvoiceStatus.ISSUED.value,
        InvoiceStatus.VOID.value,
    ],
    InvoiceStatus.PAID.value: [
        InvoiceStatus.PARTIALLY_PAID.value,
        InvoiceStatus.ISSUED.value,
    ],
    InvoiceStatus.VOID.value: [],
}


def is_transition_allowed(current: str, new: str) -> bool:
    """Whether an invoice status change is permitted."""
    return new in INVOICE_STATUS_TRANSITIONS.get(current, [])


class Invoice(TimestampedBase):
    """A bill for work performed on a vehicle."""

    __tablename__ = "invoices"

    invoice_number: Mapped[str] = mapped_column(String(30), nullable=False, unique=True, index=True)

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vehicle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # One bill per repair order: the work is done once, so it is charged once.
    repair_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repair_orders.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    estimate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("estimates.id", ondelete="SET NULL"), nullable=True, index=True
    )

    status: Mapped[str] = mapped_column(
        String(20), default=InvoiceStatus.DRAFT.value, nullable=False, index=True
    )

    invoice_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)

    # Cached line arithmetic, recomputed by the service on every change:
    #   subtotal        = sum of charge lines
    #   discount_amount = sum of discount lines
    #   tax_amount      = tax on (subtotal - discount_amount)
    #   total           = (subtotal - discount_amount) + tax_amount
    # Stored so a list of invoices can show a figure without loading its lines.
    subtotal: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False), nullable=False, default=0.0)
    discount_amount: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False), nullable=False, default=0.0
    )
    tax_rate: Mapped[float] = mapped_column(Numeric(6, 4, asdecimal=False), nullable=False, default=0.0)
    tax_amount: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False), nullable=False, default=0.0)
    total: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False), nullable=False, default=0.0)

    # How much has actually been received. Written only by the payments module;
    # the balance below is derived from it rather than stored beside it.
    amount_paid: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False), nullable=False, default=0.0
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    items: Mapped[list[InvoiceItem]] = relationship(
        "InvoiceItem",
        back_populates="invoice",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="InvoiceItem.sequence, InvoiceItem.created_at",
    )

    __table_args__ = (
        CheckConstraint("status IN " + str(INVOICE_STATUS_VALUES), name="ck_invoices_status"),
        CheckConstraint("subtotal >= 0", name="ck_invoices_subtotal"),
        CheckConstraint("discount_amount >= 0", name="ck_invoices_discount"),
        CheckConstraint("tax_rate >= 0 AND tax_rate <= 1", name="ck_invoices_tax_rate"),
        CheckConstraint("tax_amount >= 0", name="ck_invoices_tax_amount"),
        CheckConstraint("total >= 0", name="ck_invoices_total"),
        CheckConstraint("amount_paid >= 0", name="ck_invoices_amount_paid"),
        # Money received can never exceed the bill: an overpayment is a customer
        # credit, not a negative invoice, and is handled as its own transaction.
        CheckConstraint("amount_paid <= total", name="ck_invoices_not_overpaid"),
    )

    @property
    def is_editable(self) -> bool:
        """Only a draft can have its lines changed.

        Once the invoice has been sent the customer is holding a document; the
        numbers on it have to keep matching the copy they were given.
        """
        return self.status == InvoiceStatus.DRAFT.value

    @property
    def is_terminal(self) -> bool:
        """True when this invoice is void and can never be reopened."""
        return self.status in TERMINAL_INVOICE_STATUSES

    @property
    def is_void(self) -> bool:
        return self.status == InvoiceStatus.VOID.value

    @property
    def is_paid(self) -> bool:
        return self.status == InvoiceStatus.PAID.value

    @property
    def is_payable(self) -> bool:
        """True when the invoice can still take money."""
        return self.status in PAYABLE_INVOICE_STATUSES

    @property
    def balance(self) -> float:
        """What the customer still owes.

        Derived, not stored: a second column would be a second number to keep in
        step with ``total`` and ``amount_paid``, and it would be the one that
        drifts.
        """
        return round_money(max(float(self.total) - float(self.amount_paid), 0.0))

    @property
    def has_balance(self) -> bool:
        """True while any money is outstanding."""
        return self.balance > 0.0

    @property
    def item_count(self) -> int:
        return len(self.items)

    @property
    def is_overdue(self) -> bool:
        """True when the due date has passed and money is still owed.

        Both halves matter. A voided bill is not owed at all, and a bill whose
        balance has been cleared is not late however old it is — the test is
        whether a customer still owes something, not how old the paper is.
        """
        if self.due_date is None or self.is_terminal or self.status == InvoiceStatus.DRAFT.value:
            return False
        if not self.has_balance:
            return False
        return self.due_date < today()

    @property
    def days_overdue(self) -> int:
        """How many days past the due date an unpaid invoice is."""
        if not self.is_overdue or self.due_date is None:
            return 0
        return (today() - self.due_date).days

    def __repr__(self) -> str:
        return f"<Invoice({self.invoice_number} {self.status} {self.total})>"


class InvoiceItem(TimestampedBase):
    """A single charge on an invoice."""

    __tablename__ = "invoice_items"

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )

    item_type: Mapped[str] = mapped_column(
        String(20), default=InvoiceItemType.LABOR.value, nullable=False
    )
    source: Mapped[str] = mapped_column(
        String(20), default=InvoiceItemSource.MANUAL.value, nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # The estimate line this was copied from, when it came from one. SET NULL: if
    # the estimate is ever removed, the bill that was built from it must survive.
    estimate_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("estimate_items.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Identity of the part as it stood when the invoice was written, so a catalog
    # rename later cannot make a sent invoice unreadable.
    part_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    part_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    quantity: Mapped[float] = mapped_column(Numeric(10, 2, asdecimal=False), nullable=False, default=1.0)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False), nullable=False, default=0.0)
    discount_amount: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False), nullable=False, default=0.0
    )
    line_total: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False), nullable=False, default=0.0)

    # Free text naming where the line came from, e.g. the estimate number it was
    # copied from or the RO it settles.
    reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    invoice = relationship("Invoice", back_populates="items")

    __table_args__ = (
        CheckConstraint("item_type IN " + str(INVOICE_ITEM_TYPE_VALUES), name="ck_invoice_items_type"),
        CheckConstraint("source IN " + str(INVOICE_ITEM_SOURCE_VALUES), name="ck_invoice_items_source"),
        CheckConstraint("quantity > 0", name="ck_invoice_items_quantity"),
        CheckConstraint("unit_price >= 0", name="ck_invoice_items_unit_price"),
        CheckConstraint("discount_amount >= 0", name="ck_invoice_items_discount"),
        # A DISCOUNT line is stored positive and always subtracted, so its stored
        # total is negative or zero. Any other line can never reduce the bill.
        CheckConstraint(
            "(item_type = 'DISCOUNT' AND line_total <= 0) OR "
            "(item_type <> 'DISCOUNT' AND line_total >= 0)",
            name="ck_invoice_items_discount_sign",
        ),
        # A line copied from an estimate belongs to one invoice only, so the same
        # approval cannot be billed twice.
        UniqueConstraint("estimate_item_id", name="uq_invoice_items_estimate_item"),
    )

    @property
    def is_discount(self) -> bool:
        """True for lines that reduce the invoice."""
        return self.item_type == InvoiceItemType.DISCOUNT.value

    @property
    def is_from_estimate(self) -> bool:
        """True when this line is a frozen copy of an approved estimate line."""
        return self.source == InvoiceItemSource.ESTIMATE.value

    def compute_line_total(self) -> float:
        """Work out this line's net amount.

        Labor bills hours x rate; everything else bills quantity x unit price. A
        DISCOUNT line is stored as a positive amount but always subtracted, and a
        per-line ``discount_amount`` comes off a charge, never off a discount.
        """
        if self.is_discount:
            return round_money(-abs(float(self.unit_price or 0.0)))

        gross = float(self.quantity or 0.0) * float(self.unit_price or 0.0)
        net = gross - float(self.discount_amount or 0.0)
        return round_money(max(net, 0.0))

    def __repr__(self) -> str:
        return f"<InvoiceItem({self.item_type}: {self.description} = {self.line_total})>"


def default_due_date(invoice_date: date | None = None) -> date:
    """The due date an invoice gets when the caller does not state one."""
    base = invoice_date or today()
    return base + timedelta(days=DEFAULT_PAYMENT_TERM_DAYS)
