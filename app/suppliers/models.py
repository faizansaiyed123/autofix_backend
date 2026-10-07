"""Supplier data models.

A :class:`Supplier` is one business the shop buys parts from — the counterparty
on a purchase order. It records how to reach them, where they deliver, how long
they take, and whether the shop still buys from them.

``status`` is the shop's own trading decision, not the supplier's state of
business: ``INACTIVE`` means "we do not buy from this one any more", which is
different from a supplier that has gone bust. Inactive suppliers keep their
order history, so nothing that was ever ordered from them becomes untraceable.

``lead_time_days`` is quoted in whole days and used when raising a purchase order
to suggest an expected delivery date. It is a promise about the future, so it is
only a default — the date on the order itself is what the shop is held to.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TimestampedBase


class SupplierStatus(str, enum.Enum):
    """Whether the shop still buys from this supplier."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


SUPPLIER_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in SupplierStatus)


class Supplier(TimestampedBase):
    """A business the shop buys parts from."""

    __tablename__ = "suppliers"

    # Unique so a supplier cannot be entered twice and have the shop's history
    # split across two records. Compared case-insensitively by the service.
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)

    contact_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)

    address_line1: Mapped[str | None] = mapped_column(String(200), nullable=True)
    address_line2: Mapped[str | None] = mapped_column(String(200), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # The supplier's own customer/account number for this shop. Handy when
    # placing a phone order, and the thing a returned invoice is queried against.
    account_number: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    website: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # How long this supplier normally takes, used to suggest a delivery date
    # when raising an order. Zero means "next working day" in practice.
    lead_time_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # The supplier's payment terms in their own words ("Net 30", "COD"). Free
    # text on purpose: no two suppliers describe terms the same way, and the
    # text is what a person needs when deciding when to pay.
    payment_terms: Mapped[str | None] = mapped_column(String(100), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        String(20), default=SupplierStatus.ACTIVE.value, nullable=False, index=True
    )
    is_preferred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # No delete cascade: orders already placed are the shop's record of what it
    # bought and what it paid, so a supplier that has been used cannot simply be
    # deleted. Retire it with ``status=INACTIVE`` instead.
    purchase_orders = relationship(
        "PurchaseOrder",
        back_populates="supplier",
        order_by="PurchaseOrder.created_at",
    )

    __table_args__ = (
        CheckConstraint("status IN " + str(SUPPLIER_STATUS_VALUES), name="ck_suppliers_status"),
        CheckConstraint("lead_time_days >= 0", name="ck_suppliers_lead_time_days"),
    )

    @property
    def is_active(self) -> bool:
        """True while the shop still buys from this supplier."""
        return self.status == SupplierStatus.ACTIVE.value

    @property
    def address(self) -> str:
        """The postal address as one line, omitting anything not filled in."""
        lines = [
            " ".join(part for part in (self.address_line1, self.address_line2) if part),
            self.city or "",
            " ".join(part for part in (self.state, self.postal_code) if part),
            self.country or "",
        ]
        return ", ".join(line for line in lines if line)

    def __repr__(self) -> str:
        return f"<Supplier({self.name} {self.status})>"
