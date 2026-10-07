"""Purchase order data models.

A :class:`PurchaseOrder` is one order placed with one supplier: the lines
ordered, the price agreed for each, and how much of it has actually turned up.
:class:`PurchaseOrderItem` is one part on that order.

The lifecycle is deliberately small, because a purchase order only really has
three outcomes — it was sent, some or all of it arrived, or it was called off:

``DRAFT``               written but not yet sent to the supplier; editable
``SENT``                with the supplier, nothing received yet
``PARTIALLY_RECEIVED``  some lines (or part of a line) have arrived
``RECEIVED``            every line is fully received — terminal
``CANCELLED``           called off — terminal

Only a ``DRAFT`` can be edited. Once an order has been sent the supplier may
already have the goods, so changing what was ordered underneath it would make
the order and the delivery disagree. Correcting a sent order is a new order or a
cancellation, not an edit.

Quantities are tracked per line: ``quantity_ordered`` is what was asked for and
``quantity_received`` is what has been booked in. The difference is what is
still outstanding. The order's own totals are a *cache* of the line arithmetic —
recomputed by the service whenever a line or a receipt changes, so a list of
orders can show a value without loading every line of every order.

Receiving books stock through :mod:`app.inventory` as a ``RECEIPT`` transaction,
never by touching a part's balance. A purchase order says what was bought; the
ledger says what is on the shelf; neither invents the other's numbers.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

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


def round_money(value: float) -> float:
    """Round a monetary amount to cents.

    Nudged off the exact half-cent first, so ``0.125`` becomes ``0.13`` rather
    than a parity-dependent result.
    """
    return round(value + 1e-9, CENTS)


class PurchaseOrderStatus(str, enum.Enum):
    """Lifecycle of a purchase order."""

    DRAFT = "DRAFT"
    SENT = "SENT"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"


PURCHASE_ORDER_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in PurchaseOrderStatus)

# Statuses that will accept nothing further: the order is closed.
TERMINAL_PURCHASE_ORDER_STATUSES: frozenset[str] = frozenset(
    {
        PurchaseOrderStatus.RECEIVED.value,
        PurchaseOrderStatus.CANCELLED.value,
    }
)

# Statuses in which a delivery can still be booked in.
RECEIVABLE_PURCHASE_ORDER_STATUSES: frozenset[str] = frozenset(
    {
        PurchaseOrderStatus.SENT.value,
        PurchaseOrderStatus.PARTIALLY_RECEIVED.value,
    }
)

PURCHASE_ORDER_STATUS_TRANSITIONS: dict[str, list[str]] = {
    PurchaseOrderStatus.DRAFT.value: [
        PurchaseOrderStatus.SENT.value,
        PurchaseOrderStatus.CANCELLED.value,
    ],
    PurchaseOrderStatus.SENT.value: [
        PurchaseOrderStatus.PARTIALLY_RECEIVED.value,
        PurchaseOrderStatus.RECEIVED.value,
        PurchaseOrderStatus.CANCELLED.value,
    ],
    PurchaseOrderStatus.PARTIALLY_RECEIVED.value: [
        PurchaseOrderStatus.PARTIALLY_RECEIVED.value,
        PurchaseOrderStatus.RECEIVED.value,
        PurchaseOrderStatus.CANCELLED.value,
    ],
    PurchaseOrderStatus.RECEIVED.value: [],
    PurchaseOrderStatus.CANCELLED.value: [],
}


def is_transition_allowed(current: str, new: str) -> bool:
    """Whether a purchase-order status change is permitted."""
    return new in PURCHASE_ORDER_STATUS_TRANSITIONS.get(current, [])


class PurchaseOrder(TimestampedBase):
    """One order placed with one supplier."""

    __tablename__ = "purchase_orders"

    po_number: Mapped[str] = mapped_column(String(30), nullable=False, unique=True, index=True)
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False, index=True
    )

    status: Mapped[str] = mapped_column(
        String(20), default=PurchaseOrderStatus.DRAFT.value, nullable=False, index=True
    )

    order_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    expected_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)

    # Cached line arithmetic, recomputed by the service on every change:
    #   subtotal   = sum of line totals
    #   total      = subtotal + tax + shipping
    # Stored so an order list can show a total without loading its lines.
    subtotal: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False), nullable=False, default=0.0)
    tax_amount: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False), nullable=False, default=0.0)
    shipping_amount: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False), nullable=False, default=0.0
    )
    total_amount: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False), nullable=False, default=0.0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    internal_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    supplier = relationship("Supplier", back_populates="purchase_orders", lazy="selectin")
    items = relationship(
        "PurchaseOrderItem",
        back_populates="purchase_order",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="PurchaseOrderItem.line_number",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN " + str(PURCHASE_ORDER_STATUS_VALUES), name="ck_purchase_orders_status"
        ),
        CheckConstraint("subtotal >= 0", name="ck_purchase_orders_subtotal"),
        CheckConstraint("tax_amount >= 0", name="ck_purchase_orders_tax"),
        CheckConstraint("shipping_amount >= 0", name="ck_purchase_orders_shipping"),
        CheckConstraint("total_amount >= 0", name="ck_purchase_orders_total"),
    )

    @property
    def is_terminal(self) -> bool:
        """True when this order is closed and accepts nothing further."""
        return self.status in TERMINAL_PURCHASE_ORDER_STATUSES

    @property
    def is_editable(self) -> bool:
        """Only a draft can have its lines changed."""
        return self.status == PurchaseOrderStatus.DRAFT.value

    @property
    def item_count(self) -> int:
        """How many lines are on the order."""
        return len(self.items)

    @property
    def total_units_ordered(self) -> float:
        """Units across every line."""
        return round_money(sum(float(item.quantity_ordered) for item in self.items))

    @property
    def total_units_received(self) -> float:
        """Units booked in across every line."""
        return round_money(sum(float(item.quantity_received) for item in self.items))

    @property
    def is_fully_received(self) -> bool:
        """True once every line has been fully received.

        An order with no lines is never "fully received" — nothing has arrived,
        so a zero-line order must not be allowed to close itself.
        """
        return bool(self.items) and all(item.is_fully_received for item in self.items)

    @property
    def supplier_name(self) -> str | None:
        """The supplier's name, for display alongside the order."""
        return self.supplier.name if self.supplier is not None else None

    @property
    def is_overdue(self) -> bool:
        """True when the expected delivery date has passed and the order is open.

        An order that is still waiting for goods after its own promised date is
        the one to chase. Received and cancelled orders are closed, so they are
        never overdue no matter how old they are.
        """
        if self.expected_delivery_date is None or self.is_terminal:
            return False
        return self.expected_delivery_date < today()

    def __repr__(self) -> str:
        return f"<PurchaseOrder({self.po_number} {self.status})>"


class PurchaseOrderItem(TimestampedBase):
    """One part on a purchase order, with what was ordered and what arrived."""

    __tablename__ = "purchase_order_items"

    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("purchase_orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    part_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parts.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    # Display order of the line, and the part's identity as it stood when the
    # order was written. Copied because a supplier's own paperwork quotes a part
    # number, and a catalog rename later must not make a past order unreadable.
    line_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    part_number: Mapped[str] = mapped_column(String(50), nullable=False)
    part_name: Mapped[str] = mapped_column(String(200), nullable=False)

    quantity_ordered: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False), nullable=False)
    quantity_received: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False), nullable=False, default=0.0
    )

    # The price agreed with the supplier, which is not necessarily the catalog
    # cost: what the shop paid for a past delivery is history and must not move
    # when the catalog is re-priced.
    unit_cost: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False), nullable=False, default=0.0)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    purchase_order = relationship("PurchaseOrder", back_populates="items")
    part = relationship("Part")

    __table_args__ = (
        UniqueConstraint("purchase_order_id", "part_id", name="uq_purchase_order_items_part"),
        CheckConstraint("quantity_ordered > 0", name="ck_purchase_order_items_ordered"),
        CheckConstraint("quantity_received >= 0", name="ck_purchase_order_items_received"),
        CheckConstraint("unit_cost >= 0", name="ck_purchase_order_items_unit_cost"),
        # More can never be received than was ordered: an over-delivery has to be
        # agreed as a new line or an adjustment, not quietly absorbed here.
        CheckConstraint(
            "quantity_received <= quantity_ordered", name="ck_purchase_order_items_not_over"
        ),
    )

    @property
    def line_total(self) -> float:
        """What the line costs at the agreed unit price."""
        return round_money(float(self.quantity_ordered) * float(self.unit_cost))

    @property
    def received_total(self) -> float:
        """What has been received on this line so far, at the agreed price."""
        return round_money(float(self.quantity_received) * float(self.unit_cost))

    @property
    def quantity_outstanding(self) -> float:
        """Units still to arrive on this line."""
        return round_money(max(float(self.quantity_ordered) - float(self.quantity_received), 0.0))

    @property
    def is_fully_received(self) -> bool:
        """True when the whole line has arrived."""
        return float(self.quantity_received) >= float(self.quantity_ordered)

    def __repr__(self) -> str:
        return f"<PurchaseOrderItem({self.part_number} x{self.quantity_ordered})>"
