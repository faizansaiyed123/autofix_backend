"""Parts catalog data models.

A ``Part`` is one sellable line in the shop's catalog: the manufacturer's part
number, the shop's own SKU, what it costs the shop, what the customer pays, and
how many are on the shelf.

``quantity_on_hand`` is a *cached running balance*. It exists so a list of a few
hundred parts does not have to replay the whole ledger to answer "how many are
left", but it is never written to directly — every change to it is the result of
an inventory transaction, which records the before/after balance at the moment it
happened. A balance that cannot be reconciled against its ledger is an
unauditable number, so the service layer refuses any path that would produce one.

``reorder_level`` is the shop's own trigger point, not a guarantee: once the
balance falls to or below it the part shows up on the low-stock list until
somebody buys more. ``ACTIVE`` / ``DISCONTINUED`` says whether the part is still
traded at all; a discontinued part may be drawn down to zero but not restocked.
"""

from __future__ import annotations

import enum

from sqlalchemy import CheckConstraint, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TimestampedBase


class PartStatus(str, enum.Enum):
    """Whether the shop still trades this part."""

    ACTIVE = "ACTIVE"
    DISCONTINUED = "DISCONTINUED"


PART_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in PartStatus)


class StockStatus(str, enum.Enum):
    """How a part's stock compares to its reorder level."""

    OK = "OK"
    LOW = "LOW"
    OUT = "OUT"


class Part(TimestampedBase):
    """One catalog line: identity, pricing, reorder point and stock balance."""

    __tablename__ = "parts"

    # The manufacturer's number (e.g. "BOS0986A") — what a supplier quotes and
    # what a technician searches for. Unique because two catalog lines cannot
    # both be that part.
    part_number: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)

    # The shop's own stock keeping code. Optional, but unique when supplied so
    # receiving against a barcode cannot silently create a duplicate line.
    sku: Mapped[str | None] = mapped_column(String(50), nullable=True, unique=True, index=True)

    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    brand: Mapped[str | None] = mapped_column(String(100), nullable=True)
    location: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # What the shop pays, and what the customer is charged. Both are catalog
    # facts: a price change is a decision about *future* sales and must never
    # restate what past receipts actually cost, which is why each inventory
    # transaction keeps its own unit_cost.
    unit_cost: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False), nullable=False, default=0.0
    )
    unit_price: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False), nullable=False, default=0.0
    )

    # Running balance maintained by the inventory service. A part is created
    # empty and is stocked by a RECEIPT transaction, so the very first unit on
    # the shelf is on the record like every unit after it.
    quantity_on_hand: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False), nullable=False, default=0.0
    )
    reorder_level: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False), nullable=False, default=0.0
    )

    status: Mapped[str] = mapped_column(
        String(20), default=PartStatus.ACTIVE.value, nullable=False, index=True
    )

    transactions = relationship(
        "InventoryTransaction",
        back_populates="part",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="InventoryTransaction.created_at",
    )

    __table_args__ = (
        CheckConstraint("status IN " + str(PART_STATUS_VALUES), name="ck_parts_status"),
        CheckConstraint("unit_cost >= 0", name="ck_parts_unit_cost"),
        CheckConstraint("unit_price >= 0", name="ck_parts_unit_price"),
        CheckConstraint("quantity_on_hand >= 0", name="ck_parts_quantity_on_hand"),
        CheckConstraint("reorder_level >= 0", name="ck_parts_reorder_level"),
    )

    @property
    def margin(self) -> float:
        """Gross profit per unit at today's catalog price."""
        return round(float(self.unit_price) - float(self.unit_cost), 2)

    @property
    def is_out_of_stock(self) -> bool:
        """True when nothing is on the shelf."""
        return float(self.quantity_on_hand) <= 0

    @property
    def is_low_stock(self) -> bool:
        """True when the balance has fallen to or below the reorder level."""
        return float(self.quantity_on_hand) <= float(self.reorder_level)

    @property
    def stock_status(self) -> str:
        """``OK``, ``LOW`` or ``OUT`` — which end of the reorder point we are at."""
        if self.is_out_of_stock:
            return StockStatus.OUT.value
        if self.is_low_stock:
            return StockStatus.LOW.value
        return StockStatus.OK.value

    @property
    def stock_value(self) -> float:
        """What the stock on the shelf is worth at today's cost."""
        return round(float(self.quantity_on_hand) * float(self.unit_cost), 2)

    def __repr__(self) -> str:
        return f"<Part({self.part_number} {self.name} x{self.quantity_on_hand})>"
