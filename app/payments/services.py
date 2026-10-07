"""Payment business logic.

Owns the money side of an invoice: recording what was taken, refusing what could
not have been, and reversing what turns out to have been wrong.

The rule that shapes this module: **a payment is the only thing that can settle
an invoice.** Recording one and moving the invoice's balance happen in the same
database transaction, so a payment row can never exist without the invoice
knowing about it, and an invoice can never show money that was not received.

Payments are also append-only. There is no update and no delete here, and that is
deliberate rather than an oversight: a record of what a customer handed over is a
fact about the till, and voiding a payment keeps the row as the explanation of
what happened instead of erasing the mistake and the correction alike.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.common.dates import today
from app.common.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.invoices.models import Invoice, round_money
from app.invoices.services import InvoiceService
from app.payments.models import (
    REFERENCED_PAYMENT_METHODS,
    Payment,
    PaymentMethod,
    PaymentStatus,
)
from app.payments.schemas import (
    PaymentCreate,
    PaymentSummary,
    PaymentTotals,
)

logger = logging.getLogger("autofix.payments.services")

# Statuses whose payments still count towards an invoice's balance.
LIVE_PAYMENT_STATUSES: frozenset[str] = frozenset({PaymentStatus.RECORDED.value})


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PaymentService:
    """Service for recording and reversing payments."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.invoices = InvoiceService(db)

    # --- reads --------------------------------------------------------------

    async def get_by_id(self, payment_id: UUID | str) -> Payment:
        """Get a payment by ID."""
        result = await self.db.execute(
            select(Payment)
            .options(selectinload(Payment.invoice))
            .where(Payment.id == str(payment_id))
            .execution_options(populate_existing=True)
        )
        payment = result.scalar_one_or_none()
        if not payment:
            raise NotFoundError(f"Payment with id {payment_id} not found")
        return payment

    async def list_payments(
        self,
        *,
        page: int = 1,
        size: int = 20,
        invoice_id: UUID | str | None = None,
        customer_id: UUID | str | None = None,
        method: str | None = None,
        status: str | None = None,
        search: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> tuple[list[Payment], int]:
        """List payments with filtering and pagination, most recent first.

        ``customer_id`` reaches through the invoice: a payment is recorded against
        a bill, and "what has this customer paid us" is the question the counter
        actually asks.
        """
        stmt = select(Payment).options(selectinload(Payment.invoice))
        count_stmt = select(func.count()).select_from(Payment)

        filters = []
        if invoice_id:
            filters.append(Payment.invoice_id == str(invoice_id))
        if customer_id:
            filters.append(
                Payment.invoice_id.in_(
                    select(Invoice.id).where(Invoice.customer_id == str(customer_id))
                )
            )
        if method:
            try:
                filters.append(Payment.method == PaymentMethod(str(method).upper()).value)
            except ValueError:
                # An unknown method matches nothing rather than everything.
                filters.append(Payment.id.is_(None))
        if status:
            try:
                filters.append(Payment.status == PaymentStatus(str(status).upper()).value)
            except ValueError:
                filters.append(Payment.id.is_(None))
        if search:
            term = f"%{str(search).strip()}%"
            filters.append(func.coalesce(Payment.reference, "").like(term))
        if start_date:
            filters.append(Payment.payment_date >= start_date)
        if end_date:
            filters.append(Payment.payment_date <= end_date)

        for condition in filters:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        total = (await self.db.execute(count_stmt)).scalar_one()

        offset = (page - 1) * size
        stmt = (
            stmt.order_by(Payment.payment_date.desc(), Payment.created_at.desc())
            .offset(offset)
            .limit(size)
        )
        payments = list((await self.db.execute(stmt)).scalars().all())
        return payments, total

    async def get_summary(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> PaymentSummary:
        """Takings over a period, counted net of anything reversed.

        Reversed payments are subtracted rather than ignored, so a bad afternoon
        at the till shows up as what it was instead of quietly inflating the day's
        numbers.
        """
        conditions = []
        if start_date:
            conditions.append(Payment.payment_date >= start_date)
        if end_date:
            conditions.append(Payment.payment_date <= end_date)

        stmt = select(
            func.coalesce(func.sum(Payment.amount), 0.0),
            func.count(Payment.id),
        ).where(Payment.status.in_(LIVE_PAYMENT_STATUSES))
        count_stmt = select(func.count(Payment.id)).where(
            Payment.status.in_(LIVE_PAYMENT_STATUSES)
        )
        void_stmt = select(
            func.coalesce(func.sum(Payment.amount), 0.0),
            func.count(Payment.id),
        ).where(Payment.status == PaymentStatus.VOID.value)
        by_method_stmt = select(
            Payment.method, func.coalesce(func.sum(Payment.amount), 0.0)
        ).where(Payment.status.in_(LIVE_PAYMENT_STATUSES)).group_by(Payment.method)

        for condition in conditions:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)
            void_stmt = void_stmt.where(condition)
            by_method_stmt = by_method_stmt.where(condition)

        received, _ = (await self.db.execute(stmt)).one()
        payment_count = (await self.db.execute(count_stmt)).scalar_one()
        voided, void_count = (await self.db.execute(void_stmt)).one()
        by_method = {
            method: round_money(float(total))
            for method, total in (await self.db.execute(by_method_stmt)).all()
        }

        net = round_money(float(received or 0.0) - float(voided or 0.0))
        return PaymentSummary(
            start_date=start_date,
            end_date=end_date,
            totals=PaymentTotals(
                total_received=round_money(float(received or 0.0)),
                total_voided=round_money(float(voided or 0.0)),
                net_received=net,
                payment_count=int(payment_count or 0),
                void_count=int(void_count or 0),
                by_method=by_method,
            ),
        )

    async def list_for_invoice(self, invoice_id: UUID | str) -> list[Payment]:
        """Every payment recorded against one invoice, newest first."""
        result = await self.db.execute(
            select(Payment)
            .where(Payment.invoice_id == str(invoice_id))
            .order_by(Payment.payment_date.desc(), Payment.created_at.desc())
        )
        return list(result.scalars().all())

    # --- writes -------------------------------------------------------------

    async def record_payment(
        self, data: PaymentCreate, recorded_by_id: UUID | str | None = None
    ) -> Payment:
        """Record money taken against an invoice.

        The payment row and the invoice's new balance land in one transaction:
        either the shop has the money and the customer has the credit, or neither
        happened. Partial payments are ordinary — the remainder simply stays owed.
        """
        invoice = await self.invoices.get_by_id(data.invoice_id)
        self._assert_reference_present(data.method, data.reference)

        # Validated before the row is built: apply_payment refuses a draft, a
        # voided bill, or an amount above the balance, and a refused payment must
        # leave nothing behind for the next flush to try to write.
        amount = round_money(float(data.amount))
        await self.invoices.apply_payment(invoice, amount)

        payment = Payment(
            invoice_id=str(invoice.id),
            amount=amount,
            method=data.method,
            status=PaymentStatus.RECORDED.value,
            payment_date=data.payment_date or today(),
            reference=data.reference,
            notes=data.notes,
            recorded_by_id=str(recorded_by_id) if recorded_by_id else None,
        )
        self.db.add(payment)

        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("Payment conflicts with an existing record")

        logger.info(
            "Payment recorded: %s against %s (balance %s)",
            amount,
            invoice.invoice_number,
            invoice.balance,
        )
        return await self.get_by_id(payment.id)

    async def void_payment(self, payment_id: UUID | str, reason: str) -> Payment:
        """Reverse a recorded payment and put the money back on the invoice.

        The row is kept: it is the only record that the money was taken and then
        given back, and a till that quietly forgets its mistakes cannot be
        reconciled. Voiding twice is a no-op, so a double-click cannot reverse the
        same money twice.
        """
        payment = await self.get_by_id(payment_id)

        if payment.is_void:
            return payment
        if not str(reason or "").strip():
            raise BusinessRuleError("Voiding a payment requires a reason")

        invoice = await self.invoices.get_by_id(payment.invoice_id)
        await self.invoices.reverse_payment(invoice, float(payment.amount))

        payment.status = PaymentStatus.VOID.value
        payment.voided_at = _utcnow()
        payment.void_reason = str(reason).strip()

        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("Payment could not be voided")

        logger.info(
            "Payment voided: %s against %s (%s)",
            payment.amount,
            invoice.invoice_number,
            payment.void_reason,
        )
        return await self.get_by_id(payment.id)

    # --- internals ----------------------------------------------------------

    @staticmethod
    def _assert_reference_present(method: str, reference: str | None) -> None:
        """A transfer or a cheque has to carry the reference that proves it.

        Checked in the service as well as the schema, because a payment written by
        a script or an import is exactly the one nobody will notice is missing
        its slip.
        """
        if method not in {m.value for m in PaymentMethod}:
            raise BusinessRuleError(f"Invalid payment method: {method}")
        if method in REFERENCED_PAYMENT_METHODS and not str(reference or "").strip():
            raise BusinessRuleError(
                f"A {method} payment requires a reference so it can be matched to the "
                "bank statement"
            )


__all__ = [
    "LIVE_PAYMENT_STATUSES",
    "PaymentService",
]
