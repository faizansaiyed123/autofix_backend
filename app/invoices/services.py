"""Invoice business logic.

Owns the bill: raising it against finished work, copying the agreed lines onto
it, adding what the shop did on top, issuing it, and writing it off.

Two rules shape this module.

**An invoice is a snapshot, not a view.** Lines copied from the estimate are
frozen onto the invoice and may not be edited or deleted afterwards. The estimate
is the record of what the customer agreed; the invoice is the document they were
handed. If the two stayed linked, re-pricing the estimate or renaming a part
would silently change a bill that has already gone out. Correcting a copied line
means writing the draft off and raising a new invoice.

**Money moves only through :mod:`app.payments`.** The invoice holds
``amount_paid`` and this module derives ``PARTIALLY_PAID`` / ``PAID`` from it —
it never accepts a status from a caller. The generic status endpoint is
deliberately unable to reach those two states for the same reason a purchase
order cannot claim a delivery that was never booked in: an invoice may not say
it was paid when no money was recorded.
"""

from __future__ import annotations

import logging
import uuid as uuid_module
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.common.dates import today
from app.common.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.estimates.models import Estimate, EstimateItem, EstimateItemStatus
from app.invoices.models import (
    BILLABLE_RO_STATUSES,
    INVOICE_STATUS_TRANSITIONS,
    PAYABLE_INVOICE_STATUSES,
    Invoice,
    InvoiceItem,
    InvoiceItemSource,
    InvoiceItemType,
    InvoiceStatus,
    default_due_date,
    is_transition_allowed,
    round_money,
)
from app.invoices.schemas import (
    InvoiceCreate,
    InvoiceItemCreate,
    InvoiceItemUpdate,
    InvoiceSummary,
    InvoiceTotals,
    InvoiceUpdate,
)
from app.notifications.events import EventPublisher
from app.notifications.services import NotificationService, invoice_issued_event
from app.repair_orders.models import RepairOrder

logger = logging.getLogger("autofix.invoices.services")

QUANTITY_PRECISION = 2


def _round_qty(value: float) -> float:
    return round(float(value), QUANTITY_PRECISION)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class InvoiceService:
    """Service for invoices and their lines."""

    def __init__(self, db: AsyncSession):
        self.db = db
        # Notification events raised by this service, flushed with the change
        # that caused them. See app.notifications.events for why they are queued
        # rather than written inline.
        self.events = EventPublisher()

    # --- reads --------------------------------------------------------------

    async def get_by_id(self, invoice_id: UUID | str) -> Invoice:
        """Get an invoice by ID, with its lines loaded.

        ``populate_existing`` refreshes identity-mapped instances; sessions run
        with ``expire_on_commit=False``, so a re-read in the same session would
        otherwise hand back a stale line collection after a write.
        """
        result = await self.db.execute(
            select(Invoice)
            .options(selectinload(Invoice.items))
            .where(Invoice.id == str(invoice_id))
            .execution_options(populate_existing=True)
        )
        invoice = result.scalar_one_or_none()
        if not invoice:
            raise NotFoundError(f"Invoice with id {invoice_id} not found")
        return invoice

    async def get_by_number(self, invoice_number: str) -> Invoice:
        """Look an invoice up by its human-facing number."""
        result = await self.db.execute(
            select(Invoice)
            .options(selectinload(Invoice.items))
            .where(Invoice.invoice_number == str(invoice_number).strip().upper())
            .execution_options(populate_existing=True)
        )
        invoice = result.scalar_one_or_none()
        if not invoice:
            raise NotFoundError(f"Invoice {invoice_number} not found")
        return invoice

    async def list_invoices(
        self,
        *,
        page: int = 1,
        size: int = 20,
        status: str | None = None,
        customer_id: UUID | str | None = None,
        vehicle_id: UUID | str | None = None,
        repair_order_id: UUID | str | None = None,
        search: str | None = None,
        overdue_only: bool = False,
        unpaid_only: bool = False,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> tuple[list[Invoice], int]:
        """List invoices with filtering and pagination, newest first."""
        stmt = select(Invoice).options(selectinload(Invoice.items))
        count_stmt = select(func.count()).select_from(Invoice)

        filters = []
        if status:
            try:
                filters.append(Invoice.status == InvoiceStatus(str(status).upper()).value)
            except ValueError:
                # An unknown status matches nothing rather than everything.
                filters.append(Invoice.id.is_(None))
        if customer_id:
            filters.append(Invoice.customer_id == str(customer_id))
        if vehicle_id:
            filters.append(Invoice.vehicle_id == str(vehicle_id))
        if repair_order_id:
            filters.append(Invoice.repair_order_id == str(repair_order_id))
        if search:
            term = f"%{str(search).strip().upper()}%"
            filters.append(func.upper(Invoice.invoice_number).like(term))
        if start_date:
            filters.append(Invoice.invoice_date >= start_date)
        if end_date:
            filters.append(Invoice.invoice_date <= end_date)

        for condition in filters:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        if unpaid_only:
            # Money is still outstanding: an issued or part-paid invoice. A draft
            # has not been asked for yet, and a paid or voided one is closed.
            stmt = stmt.where(Invoice.status.in_(PAYABLE_INVOICE_STATUSES))
            count_stmt = count_stmt.where(Invoice.status.in_(PAYABLE_INVOICE_STATUSES))

        if overdue_only:
            # The same three tests the invoice's own is_overdue uses: a promised
            # date given, date passed, still open. Applied to both statements
            # directly because the total has to count the same rows as the page.
            for condition in (
                Invoice.due_date.is_not(None),
                Invoice.due_date < today(),
                Invoice.status.in_(PAYABLE_INVOICE_STATUSES),
            ):
                stmt = stmt.where(condition)
                count_stmt = count_stmt.where(condition)

        total = (await self.db.execute(count_stmt)).scalar_one()

        offset = (page - 1) * size
        stmt = (
            stmt.order_by(Invoice.invoice_date.desc(), Invoice.created_at.desc())
            .offset(offset)
            .limit(size)
        )
        invoices = list((await self.db.execute(stmt)).scalars().all())
        return invoices, total

    async def get_summary(self, invoice_id: UUID | str) -> InvoiceSummary:
        """Customer-facing money summary for one invoice."""
        invoice = await self.get_by_id(invoice_id)
        return InvoiceSummary(
            invoice_id=invoice.id,
            invoice_number=invoice.invoice_number,
            status=invoice.status,
            invoice_date=invoice.invoice_date,
            due_date=invoice.due_date,
            is_overdue=invoice.is_overdue,
            days_overdue=invoice.days_overdue,
            item_count=invoice.item_count,
            totals=self.totals(invoice),
        )

    @staticmethod
    def totals(invoice: Invoice) -> InvoiceTotals:
        """The money breakdown of an invoice, read straight off its header."""
        return InvoiceTotals(
            subtotal=round_money(float(invoice.subtotal)),
            discount_amount=round_money(float(invoice.discount_amount)),
            tax_rate=float(invoice.tax_rate),
            tax_amount=round_money(float(invoice.tax_amount)),
            total=round_money(float(invoice.total)),
            amount_paid=round_money(float(invoice.amount_paid)),
            balance=invoice.balance,
        )

    # --- writes -------------------------------------------------------------

    async def create_invoice(
        self, data: InvoiceCreate, created_by_id: UUID | str | None = None
    ) -> Invoice:
        """Raise a draft invoice against a finished repair order.

        The approved lines of the order's estimate are copied onto the invoice as
        frozen ``ESTIMATE`` lines, and anything the shop did on top arrives in
        ``extra_items``. One invoice per repair order: the work was done once, so
        it is charged once.
        """
        ro = await self._load_billable_repair_order(data.repair_order_id)

        existing = (
            await self.db.execute(
                select(Invoice.id).where(Invoice.repair_order_id == str(ro.id))
            )
        ).scalar_one_or_none()
        if existing:
            raise ConflictError(
                f"Repair order {ro.ro_number} has already been invoiced; edit or void "
                "that invoice instead of raising a second bill for the same work"
            )

        estimate = await self._load_estimate(ro.estimate_id)

        invoice_date = data.invoice_date or today()
        due_date = data.due_date or default_due_date(invoice_date)
        if due_date < invoice_date:
            raise BusinessRuleError("An invoice cannot be due before it was issued")

        # A bill taxes what the estimate taxed unless the desk says otherwise:
        # the customer was quoted a rate, and changing it on the invoice is a
        # decision that has to be made on purpose.
        tax_rate = data.tax_rate
        if tax_rate is None:
            tax_rate = float(estimate.tax_rate) if estimate is not None else 0.0

        invoice = Invoice(
            invoice_number=self._new_invoice_number(),
            customer_id=str(ro.customer_id),
            vehicle_id=str(ro.vehicle_id),
            repair_order_id=str(ro.id),
            estimate_id=str(estimate.id) if estimate is not None else None,
            status=InvoiceStatus.DRAFT.value,
            invoice_date=invoice_date,
            due_date=due_date,
            tax_rate=tax_rate,
            amount_paid=0.0,
            notes=data.notes,
            customer_notes=data.customer_notes,
            created_by_id=str(created_by_id) if created_by_id else None,
            # Seeding the collection through the constructor loads it up front.
            # Appending afterwards would touch the still-unloaded relationship
            # and trigger lazy IO outside the async greenlet context.
            items=self._build_lines(estimate, data.extra_items),
        )
        self.db.add(invoice)
        # The flush assigns the invoice its UUID and the lines their foreign key;
        # the totals are only meaningful once the lines are attached to it.
        await self.db.flush()

        self.recalculate(invoice)
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("An invoice for this repair order already exists")

        logger.info(
            "Invoice raised: %s for %s with %s line(s)",
            invoice.invoice_number,
            ro.ro_number,
            len(invoice.items),
        )
        return await self.get_by_id(invoice.id)

    async def update_invoice(
        self, invoice_id: UUID | str, update_data: InvoiceUpdate
    ) -> Invoice:
        """Edit a draft invoice's dates, tax rate or notes."""
        invoice = await self.get_by_id(invoice_id)
        self._assert_editable(invoice)

        changes = update_data.model_dump(exclude_unset=True)
        if not changes:
            return invoice

        # Validate the prospective header before mutating. Raising after a partial
        # write would leave the session dirty, and the next flush would then
        # persist a change the caller never meant to make.
        invoice_date = changes.get("invoice_date", invoice.invoice_date)
        due_date = changes.get("due_date", invoice.due_date)
        if due_date and due_date < invoice_date:
            raise BusinessRuleError("An invoice cannot be due before it was issued")

        for field, value in changes.items():
            setattr(invoice, field, value)

        return await self._recalculate_and_save(invoice)

    async def add_item(
        self, invoice_id: UUID | str, item_data: InvoiceItemCreate
    ) -> Invoice:
        """Add a shop-raised charge line to a draft invoice."""
        invoice = await self.get_by_id(invoice_id)
        self._assert_editable(invoice)

        next_sequence = max((i.sequence for i in invoice.items), default=-1) + 1
        invoice.items.append(self._build_manual_line(item_data, next_sequence))

        return await self._recalculate_and_save(invoice)

    async def update_item(
        self,
        invoice_id: UUID | str,
        item_id: UUID | str,
        item_data: InvoiceItemUpdate,
    ) -> Invoice:
        """Correct a shop-raised charge line on a draft invoice.

        A line copied from the estimate is not editable: it is the customer's
        approval frozen onto the bill, and rewriting it here would let the shop
        change what was agreed without the estimate ever saying so.
        """
        invoice = await self.get_by_id(invoice_id)
        self._assert_editable(invoice)

        item = self._find_item(invoice, item_id)
        if item.is_from_estimate:
            raise BusinessRuleError(
                f"Line '{item.description}' was copied from the estimate and cannot be "
                "edited. Void this invoice and raise a new one to change the agreed work"
            )

        changes = item_data.model_dump(exclude_unset=True)
        for field, value in changes.items():
            setattr(item, field, value)
        if item.is_discount:
            # A discount is a single amount, so the quantity is fixed rather than
            # a free field that could imply "two discounts off".
            item.quantity = 1.0

        return await self._recalculate_and_save(invoice)

    async def delete_item(self, invoice_id: UUID | str, item_id: UUID | str) -> Invoice:
        """Remove a shop-raised charge line from a draft invoice."""
        invoice = await self.get_by_id(invoice_id)
        self._assert_editable(invoice)

        item = self._find_item(invoice, item_id)
        if item.is_from_estimate:
            raise BusinessRuleError(
                f"Line '{item.description}' was copied from the estimate and cannot be "
                "removed. Void this invoice and raise a new one to change the agreed work"
            )

        # Removing from the collection keeps in-session state consistent; the
        # delete-orphan cascade issues the DELETE.
        invoice.items.remove(item)
        return await self._recalculate_and_save(invoice)

    async def issue(self, invoice_id: UUID | str) -> Invoice:
        """Send a draft invoice to the customer.

        Issuing is the point of no return for the lines: the customer now holds a
        document, so the invoice becomes read-only and the balance becomes owed.
        """
        invoice = await self.get_by_id(invoice_id)
        self._assert_editable(invoice)

        if not invoice.items:
            raise BusinessRuleError("Cannot issue an invoice with no line items")
        if round_money(float(invoice.total)) <= 0:
            raise BusinessRuleError(
                "Cannot issue an invoice for zero; there is nothing to charge"
            )

        self._transition(invoice, InvoiceStatus.ISSUED.value)
        invoice.issued_at = _utcnow()
        saved = await self._recalculate_and_save(invoice)

        # Published after the issue has committed, so a failed issue leaves no
        # queued event behind for a later save to deliver.
        self.events.publish(
            invoice_issued_event(
                saved.customer_id, saved.invoice_number, saved.id
            )
        )
        await self._flush_events()
        return saved

    async def _flush_events(self) -> None:
        """Write any queued notifications, never failing the caller's operation.

        A customer not being told about their bill is a real problem, but it is
        not a reason to fail an invoice that was issued correctly — and the bill
        is on screen either way.
        """
        if not len(self.events):
            return
        try:
            await NotificationService(self.db).flush_events(self.events)
        except Exception:
            await self.db.rollback()
            logger.exception(
                "Failed to deliver notifications for invoice changes; "
                "the invoice itself is unaffected"
            )

    async def void_invoice(self, invoice_id: UUID | str, reason: str) -> Invoice:
        """Write an issued invoice off, with a reason on the record.

        Money already received blocks this: a payment is a fact about the
        customer's account, and voiding the bill underneath it would leave cash
        taken against nothing. Settle or refund the payment first.
        """
        invoice = await self.get_by_id(invoice_id)

        if invoice.status == InvoiceStatus.VOID.value:
            return invoice
        if invoice.is_editable:
            # A draft was never sent, so it is simply deleted rather than voided.
            raise BusinessRuleError(
                f"Invoice {invoice.invoice_number} is a draft and has not been issued; "
                "delete it instead of voiding it"
            )
        if not str(reason or "").strip():
            raise BusinessRuleError("Voiding an invoice requires a reason")

        if round_money(float(invoice.amount_paid)) > 0:
            raise BusinessRuleError(
                f"Invoice {invoice.invoice_number} has "
                f"{round_money(float(invoice.amount_paid))} recorded against it and cannot "
                "be voided; settle the payment first"
            )

        self._transition(invoice, InvoiceStatus.VOID.value)
        invoice.voided_at = _utcnow()
        invoice.void_reason = str(reason).strip()
        logger.info("Invoice voided: %s (%s)", invoice.invoice_number, invoice.void_reason)
        return await self._recalculate_and_save(invoice)

    async def delete_invoice(self, invoice_id: UUID | str) -> None:
        """Delete a draft invoice the customer has not been sent."""
        invoice = await self.get_by_id(invoice_id)
        if not invoice.is_editable:
            raise BusinessRuleError(
                f"Invoice {invoice.invoice_number} has been issued and cannot be deleted; "
                "void it instead"
            )
        if round_money(float(invoice.amount_paid)) > 0:
            raise BusinessRuleError(
                f"Invoice {invoice.invoice_number} has money recorded against it and "
                "cannot be deleted"
            )
        await self.db.delete(invoice)
        await self.db.commit()
        logger.info("Invoice deleted: %s", invoice.invoice_number)

    async def apply_payment(self, invoice: Invoice, amount: float) -> Invoice:
        """Record money received against an invoice.

        Called by :mod:`app.payments` once a payment row exists; the invoice's
        ``amount_paid`` and payment status are derived from it here so that no
        other code path can mark a bill paid without money behind it.
        """
        value = round_money(float(amount))
        if value <= 0:
            raise BusinessRuleError("A payment must be greater than zero")

        if invoice.is_void:
            raise BusinessRuleError(
                f"Invoice {invoice.invoice_number} is VOID and cannot take a payment"
            )
        if not invoice.is_payable:
            raise BusinessRuleError(
                f"Invoice {invoice.invoice_number} is {invoice.status}; only an ISSUED "
                "invoice can take a payment"
            )
        if value > invoice.balance:
            raise BusinessRuleError(
                f"Payment of {value} exceeds the {invoice.balance} balance on "
                f"{invoice.invoice_number}; record the difference as a credit instead"
            )

        invoice.amount_paid = round_money(float(invoice.amount_paid) + value)
        if invoice.has_balance:
            self._transition(invoice, InvoiceStatus.PARTIALLY_PAID.value)
        else:
            self._transition(invoice, InvoiceStatus.PAID.value)
            invoice.paid_at = _utcnow()
            logger.info("Invoice settled in full: %s", invoice.invoice_number)
        return invoice

    async def reverse_payment(self, invoice: Invoice, amount: float) -> Invoice:
        """Take money back off an invoice after a payment is voided.

        The mirror image of :meth:`apply_payment`, and the only way a settled
        invoice starts owing money again. The document itself is untouched — an
        issued invoice stays issued, it simply carries a balance once more.
        """
        value = round_money(float(amount))
        if value <= 0:
            raise BusinessRuleError("A reversal must be greater than zero")
        if value > round_money(float(invoice.amount_paid)):
            raise BusinessRuleError(
                f"Cannot reverse {value} from {invoice.invoice_number}: only "
                f"{round_money(float(invoice.amount_paid))} has been received"
            )

        invoice.amount_paid = round_money(float(invoice.amount_paid) - value)
        if invoice.amount_paid <= 0:
            self._transition(invoice, InvoiceStatus.ISSUED.value)
            invoice.paid_at = None
        else:
            self._transition(invoice, InvoiceStatus.PARTIALLY_PAID.value)
            invoice.paid_at = None
        return invoice

    # --- internals ----------------------------------------------------------

    @staticmethod
    def recalculate(invoice: Invoice) -> None:
        """Recompute every line total and the invoice's money totals.

        Called after any change to lines, tax rate or dates so the stored totals
        can never drift from the lines that produced them. Nothing here is
        accepted from a caller: the header is the line arithmetic plus tax, so it
        cannot disagree with its own lines.
        """
        charges = 0.0
        discounts = 0.0
        for item in invoice.items:
            item.line_total = item.compute_line_total()
            if item.is_discount:
                discounts += abs(item.line_total)
            else:
                charges += item.line_total

        subtotal = round_money(charges)
        # Discounts can never exceed the work they discount; an invoice floors at
        # zero rather than billing the customer a negative total.
        discount_amount = min(round_money(discounts), max(subtotal, 0.0))
        taxable = round_money(max(subtotal - discount_amount, 0.0))
        tax_amount = round_money(taxable * float(invoice.tax_rate))

        invoice.subtotal = max(subtotal, 0.0)
        invoice.discount_amount = discount_amount
        invoice.tax_amount = tax_amount
        invoice.total = round_money(taxable + tax_amount)

    async def _recalculate_and_save(self, invoice: Invoice) -> Invoice:
        """Recalculate and commit an invoice, then re-read it."""
        self.recalculate(invoice)
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("Invoice update conflicts with another record")
        return await self.get_by_id(invoice.id)

    @staticmethod
    def _new_invoice_number() -> str:
        """Generate a human-readable, collision-resistant invoice number.

        A date prefix makes the number recognisable on a customer's paperwork; the
        random suffix keeps concurrent creates from colliding, and the column is
        unique so any residual clash surfaces as a 409 rather than silent
        duplication.
        """
        stamp = datetime.now(UTC).strftime("%Y%m")
        suffix = uuid_module.uuid4().hex[:6].upper()
        return f"INV-{stamp}-{suffix}"

    async def _load_billable_repair_order(self, repair_order_id: UUID | str) -> RepairOrder:
        """Load the repair order being billed, rejecting work that is not finished.

        A bill is raised against work that has been through quality control, so a
        defect found by QC is put right before the customer is asked to pay for
        it. A cancelled order is never billable.
        """
        result = await self.db.execute(
            select(RepairOrder).where(RepairOrder.id == str(repair_order_id))
        )
        ro = result.scalar_one_or_none()
        if not ro:
            raise NotFoundError(f"Repair order with id {repair_order_id} not found")
        if ro.status not in BILLABLE_RO_STATUSES:
            raise BusinessRuleError(
                f"Repair order {ro.ro_number} is {ro.status}; only work that has passed "
                "quality control can be invoiced"
            )
        return ro

    async def _load_estimate(self, estimate_id: UUID | str | None) -> Estimate | None:
        """Load the estimate an invoice will be priced from, if there is one."""
        if not estimate_id:
            return None
        result = await self.db.execute(
            select(Estimate)
            .options(selectinload(Estimate.items))
            .where(Estimate.id == str(estimate_id))
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _build_lines(estimate: Estimate | None, extra_items) -> list[InvoiceItem]:
        """Build every line a new invoice starts with, in quoted order.

        The approved estimate lines come first, in the order they were quoted, so
        the bill reads like the estimate the customer signed; the shop's own
        additions follow underneath.
        """
        lines: list[InvoiceItem] = []
        sequence = 0

        if estimate is not None:
            reference = f"Estimate {estimate.estimate_number}"
            for item in estimate.items:
                if item.status != EstimateItemStatus.APPROVED.value:
                    continue
                lines.append(
                    InvoiceService._build_estimate_line(item, sequence, reference)
                )
                sequence += 1

        for item_data in extra_items:
            lines.append(InvoiceService._build_manual_line(item_data, sequence))
            sequence += 1

        return lines

    @staticmethod
    def _build_estimate_line(
        item: EstimateItem, sequence: int, reference: str
    ) -> InvoiceItem:
        """Freeze one approved estimate line onto the invoice.

        Labor is copied as hours x rate rather than as a single amount, so the
        invoice still reads as the work it is charging for. Only approved lines
        are copied: a declined line is not a debt, and a line still awaiting the
        customer's decision has not been agreed.
        """
        is_labor = item.item_type == InvoiceItemType.LABOR.value
        line = InvoiceItem(
            item_type=item.item_type,
            source=InvoiceItemSource.ESTIMATE.value,
            description=item.description,
            sequence=sequence,
            estimate_item_id=str(item.id),
            part_number=item.part_number,
            part_name=item.part_name,
            quantity=_round_qty(
                item.labor_hours if is_labor and item.labor_hours else item.quantity
            ),
            unit_price=round_money(
                item.labor_rate if is_labor and item.labor_rate else item.unit_price
            ),
            discount_amount=round_money(item.discount_amount),
            reference=reference,
            notes=item.notes,
        )
        # Computed up front so the INSERT carries the real amount rather than a
        # zero that a follow-up UPDATE has to correct.
        line.line_total = line.compute_line_total()
        return line

    @staticmethod
    def _build_manual_line(item_data: InvoiceItemCreate, sequence: int) -> InvoiceItem:
        """Build a shop-raised charge line (not yet added to the session)."""
        line = InvoiceItem(
            item_type=item_data.item_type,
            source=InvoiceItemSource.MANUAL.value,
            description=item_data.description,
            sequence=sequence,
            quantity=_round_qty(item_data.quantity),
            unit_price=round_money(item_data.unit_price),
            discount_amount=round_money(item_data.discount_amount),
            part_number=item_data.part_number,
            part_name=item_data.part_name,
            notes=item_data.notes,
        )
        line.line_total = line.compute_line_total()
        return line

    @staticmethod
    def _find_item(invoice: Invoice, item_id: UUID | str) -> InvoiceItem:
        item = next((i for i in invoice.items if str(i.id) == str(item_id)), None)
        if not item:
            raise NotFoundError(f"Line {item_id} not found on invoice {invoice.invoice_number}")
        return item

    @staticmethod
    def _assert_editable(invoice: Invoice) -> None:
        if not invoice.is_editable:
            raise BusinessRuleError(
                f"Invoice {invoice.invoice_number} is {invoice.status}; only a DRAFT "
                "invoice can be changed. Void it and raise a new one"
            )

    @staticmethod
    def _transition(invoice: Invoice, new_status: str) -> None:
        """Apply a status change, enforcing the invoice state machine."""
        if invoice.status == new_status:
            return
        if not is_transition_allowed(invoice.status, new_status):
            allowed = INVOICE_STATUS_TRANSITIONS.get(invoice.status, [])
            raise BusinessRuleError(
                f"Cannot transition invoice status from '{invoice.status}' to "
                f"'{new_status}'. Allowed: {allowed or 'none (terminal state)'}"
            )
        invoice.status = new_status


__all__ = [
    "BILLABLE_RO_STATUSES",
    "InvoiceItemSource",
    "InvoiceItemType",
    "InvoiceService",
    "InvoiceStatus",
]
