"""Payment data models.

A :class:`Payment` is one sum of money taken against one invoice: how much, how
it was paid, and whatever reference the bank or the till gives so the two can be
reconciled at the end of the day.

Payments are append-only. There is no edit and no delete, because a record of
what a customer handed over is a fact, and a fact nobody can quietly change is the
whole point of having written it down. A payment that turns out to be wrong is
**voided** — the row survives with a reason and a timestamp, and the money goes
back onto the invoice's balance. Voiding writes history; deleting erases it.

``status`` therefore has exactly two values, and one of them is terminal:

``RECORDED``  money received and counted
``VOID``      the payment was reversed; the row stays as the explanation

The invoice's own ``amount_paid`` is the sum of its *recorded* payments, and its
status is derived from that by :class:`~app.invoices.services.InvoiceService`, so
no other code path can mark a bill paid.
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
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TimestampedBase

CENTS = 2


def round_money(value: float) -> float:
    """Round a monetary amount to cents.

    Nudged off the exact half-cent first, so ``0.125`` becomes ``0.13`` rather
    than a parity-dependent result.
    """
    return round(value + 1e-9, CENTS)


class PaymentMethod(str, enum.Enum):
    """How the money arrived."""

    CASH = "CASH"
    CARD = "CARD"
    BANK_TRANSFER = "BANK_TRANSFER"
    CHEQUE = "CHEQUE"
    OTHER = "OTHER"


class PaymentStatus(str, enum.Enum):
    """Whether a recorded payment still counts."""

    RECORDED = "RECORDED"
    VOID = "VOID"


PAYMENT_METHOD_VALUES: tuple[str, ...] = tuple(m.value for m in PaymentMethod)
PAYMENT_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in PaymentStatus)

# Methods that leave a paper trail outside the till. A bank transfer or a cheque
# with no reference cannot be matched to a statement, so it cannot be reconciled
# at month end — which is exactly when somebody discovers it was never paid.
REFERENCED_PAYMENT_METHODS: frozenset[str] = frozenset(
    {
        PaymentMethod.BANK_TRANSFER.value,
        PaymentMethod.CHEQUE.value,
    }
)


class Payment(TimestampedBase):
    """Money taken against an invoice."""

    __tablename__ = "payments"

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    amount: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False), nullable=False)
    method: Mapped[str] = mapped_column(
        String(20), default=PaymentMethod.CASH.value, nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), default=PaymentStatus.RECORDED.value, nullable=False, index=True
    )

    payment_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # The till slip, terminal authorisation or bank reference that proves this
    # money arrived. Required for the methods that leave no other trace.
    reference: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    recorded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    invoice = relationship("Invoice", lazy="selectin")

    __table_args__ = (
        CheckConstraint("method IN " + str(PAYMENT_METHOD_VALUES), name="ck_payments_method"),
        CheckConstraint("status IN " + str(PAYMENT_STATUS_VALUES), name="ck_payments_status"),
        # A payment is money handed over: there is no such thing as a zero or a
        # negative payment, and a reversal is a void, not a minus.
        CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
    )

    @property
    def is_void(self) -> bool:
        """True when this payment has been reversed and no longer counts."""
        return self.status == PaymentStatus.VOID.value

    @property
    def counts_towards_balance(self) -> bool:
        """True when this payment is part of the invoice's ``amount_paid``."""
        return self.status == PaymentStatus.RECORDED.value

    @property
    def needs_reference(self) -> bool:
        """True for the methods that cannot be reconciled without a reference."""
        return self.method in REFERENCED_PAYMENT_METHODS

    def __repr__(self) -> str:
        return f"<Payment({self.amount} by {self.method} {self.status})>"
