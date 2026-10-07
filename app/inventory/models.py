"""Inventory ledger data models.

Every unit that enters or leaves the shop is written here, exactly once, and the
balance on a :class:`~app.parts.models.Part` is the sum of the rows against it.
The ledger is deliberately append-only: a mistake is corrected by a new
``ADJUSTMENT`` or by reversing the movement, never by editing history, because
"how many brake pads do we have and why" has to be answerable months later.

Transaction types
-----------------
``RECEIPT``     stock arrived (goods in, purchase return, supplier delivery)
``ISSUE``       stock consumed by a repair order
``RETURN``      an issued or sold unit came back
``ADJUSTMENT``  a correction after a stock take; signed either way
``TRANSFER``    stock moved between bins; signed (out of one bin, into another)
``SCRAP``       stock written off as damaged or unsellable

Quantity is stored *signed*: the magnitude is how many units moved and the sign
is which way. Callers supply the magnitude and the direction only when the type
does not imply it — ``RECEIPT`` and ``RETURN`` can only add, ``ISSUE`` and
``SCRAP`` can only remove, so those four reject a contradicting direction.
``ADJUSTMENT`` and ``TRANSFER`` are corrections to a balance rather than events
with an inherent direction, so they must say which way they go.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TimestampedBase


class InventoryTransactionType(str, enum.Enum):
    """The kinds of stock movement the shop records."""

    RECEIPT = "RECEIPT"
    ISSUE = "ISSUE"
    RETURN = "RETURN"
    ADJUSTMENT = "ADJUSTMENT"
    TRANSFER = "TRANSFER"
    SCRAP = "SCRAP"


class StockMovementDirection(str, enum.Enum):
    """Which way a movement moves the balance."""

    IN = "IN"
    OUT = "OUT"


INVENTORY_TRANSACTION_TYPE_VALUES: tuple[str, ...] = tuple(
    t.value for t in InventoryTransactionType
)

# Types that only ever add stock: goods do not arrive out of the door, so
# saying so is a mistake worth reporting rather than quietly obeying.
INCREASING_TRANSACTION_TYPES: frozenset[str] = frozenset(
    {
        InventoryTransactionType.RECEIPT.value,
        InventoryTransactionType.RETURN.value,
    }
)

# Types that only ever remove stock.
DECREASING_TRANSACTION_TYPES: frozenset[str] = frozenset(
    {
        InventoryTransactionType.ISSUE.value,
        InventoryTransactionType.SCRAP.value,
    }
)

# Types whose direction has to be stated, because the event itself does not
# imply one: a correction can go either way, and a bin move can arrive or leave.
SIGNED_TRANSACTION_TYPES: frozenset[str] = frozenset(
    {
        InventoryTransactionType.ADJUSTMENT.value,
        InventoryTransactionType.TRANSFER.value,
    }
)


def direction_for(transaction_type: str, direction: str | None = None) -> int:
    """The effect a transaction has on the balance: ``+1`` or ``-1``.

    Raises ``ValueError`` when the stated direction is missing for a type that
    needs one, or contradicts a type that already implies it.
    """
    if transaction_type in INCREASING_TRANSACTION_TYPES:
        if direction is not None and direction != StockMovementDirection.IN.value:
            raise ValueError(
                f"A {transaction_type} adds stock, so direction must be IN "
                "(omit it to use the type's own direction)"
            )
        return 1

    if transaction_type in DECREASING_TRANSACTION_TYPES:
        if direction is not None and direction != StockMovementDirection.OUT.value:
            raise ValueError(
                f"A {transaction_type} removes stock, so direction must be OUT "
                "(omit it to use the type's own direction)"
            )
        return -1

    if transaction_type in SIGNED_TRANSACTION_TYPES:
        if direction not in (StockMovementDirection.IN.value, StockMovementDirection.OUT.value):
            raise ValueError(
                f"A {transaction_type} can go either way, so direction must be "
                "stated explicitly (IN or OUT)"
            )
        return 1 if direction == StockMovementDirection.IN.value else -1

    raise ValueError(f"Unknown inventory transaction type: {transaction_type}")


class InventoryTransaction(TimestampedBase):
    """One movement of a part's stock, with the balance either side of it."""

    __tablename__ = "inventory_transactions"

    part_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    transaction_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    # Signed: negative removes stock. `quantity_before` / `quantity_after` are
    # copied in at write time so a single row explains the movement on its own,
    # even if the balance is later rebuilt from scratch.
    quantity: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False), nullable=False)
    quantity_before: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False), nullable=False)
    quantity_after: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False), nullable=False)

    # What this movement was worth, captured at the time. The catalog's
    # unit_cost is today's price and may have moved since, so receipts keep
    # their own figure and the ledger can be valued as it actually was.
    unit_cost: Mapped[float | None] = mapped_column(Numeric(12, 2, asdecimal=False), nullable=True)

    # What the movement was for: the repair order that consumed the part, a
    # purchase order number, a stock take reference. Kept as a free-text
    # reference so the ledger is not coupled to every future document type.
    repair_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repair_orders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reference: Mapped[str | None] = mapped_column(String(100), nullable=True)

    performed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Bin movement: a TRANSFER out names the destination, a TRANSFER in names
    # the source, so the two legs of a move can be tied together.
    from_location: Mapped[str | None] = mapped_column(String(50), nullable=True)
    to_location: Mapped[str | None] = mapped_column(String(50), nullable=True)

    part = relationship("Part", back_populates="transactions")

    __table_args__ = (
        CheckConstraint(
            "transaction_type IN " + str(INVENTORY_TRANSACTION_TYPE_VALUES),
            name="ck_inventory_transactions_type",
        ),
        CheckConstraint("quantity <> 0", name="ck_inventory_transactions_quantity"),
        CheckConstraint("quantity_before >= 0", name="ck_inventory_transactions_before"),
        CheckConstraint("quantity_after >= 0", name="ck_inventory_transactions_after"),
    )

    @property
    def movement(self) -> float:
        """The size of the movement, ignoring its direction."""
        return abs(float(self.quantity))

    @property
    def is_increase(self) -> bool:
        """True when this movement added stock."""
        return float(self.quantity) > 0

    @property
    def value(self) -> float:
        """What the movement was worth, using its captured cost."""
        cost = float(self.unit_cost) if self.unit_cost is not None else 0.0
        return round(abs(float(self.quantity)) * cost, 2)

    def __repr__(self) -> str:
        sign = "+" if self.is_increase else "-"
        return f"<InventoryTransaction({self.transaction_type} {sign}{self.movement} of {self.part_id})>"
