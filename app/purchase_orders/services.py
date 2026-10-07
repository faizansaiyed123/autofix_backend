"""Purchase order business logic.

Owns the buying workflow: raising an order against a supplier, editing it while
it is still a draft, sending it, and booking in what arrives.

The one rule that shapes this module: **receiving never touches a part's stock
balance directly.** Every line that arrives is filed as a ``RECEIPT`` in the
inventory ledger through :class:`~app.inventory.services.InventoryService`, so
goods bought from a supplier sit on the same audit trail as goods issued to a
repair order or adjusted at a stock take. A purchase order records what was
bought; the ledger records what is on the shelf; neither invents the other's
numbers.

A whole delivery is booked in as one database transaction. All the lines of one
receipt, the received counts on the order, and the order's new status land
together, or none of them do — a delivery cannot be half-booked because the
third line hit a database error.

Only a ``DRAFT`` is editable. Once an order has been sent the supplier may
already hold the goods, so rewriting its lines underneath them would leave the
order and the delivery disagreeing. Correcting a sent order means cancelling it
and raising another.
"""

from __future__ import annotations

import logging
import math
import uuid as uuid_module
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import Integer, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.common.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.inventory.models import InventoryTransactionType
from app.inventory.schemas import InventoryTransactionCreate
from app.inventory.services import InventoryService
from app.parts.models import Part, PartStatus
from app.purchase_orders.models import (
    PURCHASE_ORDER_STATUS_TRANSITIONS,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    is_transition_allowed,
    round_money,
    today,
)
from app.purchase_orders.schemas import (
    PurchaseOrderCreate,
    PurchaseOrderFromLowStock,
    PurchaseOrderItemCreate,
    PurchaseOrderItemUpdate,
    PurchaseOrderSummary,
    PurchaseOrderUpdate,
    ReceiptReference,
    ReceiveLine,
    ReceiveRequest,
    ReceiveResult,
)
from app.suppliers.services import SupplierService

logger = logging.getLogger("autofix.purchase_orders.services")

# Order statuses awaiting stock: something to chase, or stock still on its way.
OPEN_PURCHASE_ORDER_STATUSES: frozenset[str] = frozenset(
    {
        PurchaseOrderStatus.DRAFT.value,
        PurchaseOrderStatus.SENT.value,
        PurchaseOrderStatus.PARTIALLY_RECEIVED.value,
    }
)

QUANTITY_PRECISION = 2


def _round_qty(value: float) -> float:
    return round(float(value), QUANTITY_PRECISION)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PurchaseOrderService:
    """Service for purchase orders and goods receiving."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.inventory = InventoryService(db)
        self.suppliers = SupplierService(db)

    # --- reads --------------------------------------------------------------

    async def get_by_id(self, purchase_order_id: UUID | str) -> PurchaseOrder:
        """Get a purchase order by ID, with its lines and supplier loaded.

        ``populate_existing`` refreshes identity-mapped instances; sessions run
        with ``expire_on_commit=False``, so a re-read in the same session would
        otherwise hand back a stale line collection after a write.
        """
        result = await self.db.execute(
            select(PurchaseOrder)
            .options(selectinload(PurchaseOrder.items), selectinload(PurchaseOrder.supplier))
            .where(PurchaseOrder.id == str(purchase_order_id))
            .execution_options(populate_existing=True)
        )
        po = result.scalar_one_or_none()
        if not po:
            raise NotFoundError(f"Purchase order with id {purchase_order_id} not found")
        return po

    async def get_by_number(self, po_number: str) -> PurchaseOrder:
        """Look a purchase order up by its human-facing number."""
        result = await self.db.execute(
            select(PurchaseOrder)
            .options(selectinload(PurchaseOrder.items), selectinload(PurchaseOrder.supplier))
            .where(PurchaseOrder.po_number == str(po_number).strip().upper())
            .execution_options(populate_existing=True)
        )
        po = result.scalar_one_or_none()
        if not po:
            raise NotFoundError(f"Purchase order {po_number} not found")
        return po

    async def list_purchase_orders(
        self,
        *,
        page: int = 1,
        size: int = 20,
        status: str | None = None,
        supplier_id: UUID | str | None = None,
        overdue_only: bool = False,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> tuple[list[PurchaseOrder], int]:
        """List purchase orders with filtering and pagination, newest first."""
        stmt = select(PurchaseOrder).options(
            selectinload(PurchaseOrder.items), selectinload(PurchaseOrder.supplier)
        )
        count_stmt = select(func.count()).select_from(PurchaseOrder)

        filters = []
        if status:
            try:
                filters.append(
                    PurchaseOrder.status == PurchaseOrderStatus(str(status).upper()).value
                )
            except ValueError:
                # An unknown status matches nothing rather than everything.
                filters.append(PurchaseOrder.id.is_(None))
        if supplier_id:
            filters.append(PurchaseOrder.supplier_id == str(supplier_id))
        if start_date:
            filters.append(PurchaseOrder.order_date >= start_date)
        if end_date:
            filters.append(PurchaseOrder.order_date <= end_date)

        for condition in filters:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        if overdue_only:
            # The same three tests the order's own is_overdue uses: promised date
            # given, date passed, order still open. Applied to both statements
            # directly because the total has to count the same rows as the page.
            for condition in (
                PurchaseOrder.expected_delivery_date.is_not(None),
                PurchaseOrder.expected_delivery_date < today(),
                PurchaseOrder.status.in_(OPEN_PURCHASE_ORDER_STATUSES),
            ):
                stmt = stmt.where(condition)
                count_stmt = count_stmt.where(condition)

        total = (await self.db.execute(count_stmt)).scalar_one()

        offset = (page - 1) * size
        stmt = (
            stmt.order_by(PurchaseOrder.order_date.desc(), PurchaseOrder.created_at.desc())
            .offset(offset)
            .limit(size)
        )
        orders = list((await self.db.execute(stmt)).scalars().all())
        return orders, total

    async def get_summary(self) -> PurchaseOrderSummary:
        """Headline numbers across the order book, for a dashboard tile.

        ``total_committed`` is what the shop is on the hook for on orders that
        have not been called off; ``total_received_value`` is what it has already
        had to pay for, counted from the orders that actually took delivery.
        """
        overdue = (
            PurchaseOrder.status.in_(OPEN_PURCHASE_ORDER_STATUSES)
            & PurchaseOrder.expected_delivery_date.is_not(None)
            & (PurchaseOrder.expected_delivery_date < today())
        )
        counted = PurchaseOrder.status.in_(OPEN_PURCHASE_ORDER_STATUSES)
        received = PurchaseOrder.status.in_(
            [
                PurchaseOrderStatus.RECEIVED.value,
                PurchaseOrderStatus.PARTIALLY_RECEIVED.value,
            ]
        )

        row = (
            await self.db.execute(
                select(
                    func.count(PurchaseOrder.id),
                    func.coalesce(
                        func.sum(
                            func.cast(
                                PurchaseOrder.status == PurchaseOrderStatus.DRAFT.value,
                                Integer,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(func.sum(func.cast(counted, Integer)), 0),
                    func.coalesce(
                        func.sum(
                            func.cast(
                                PurchaseOrder.status == PurchaseOrderStatus.RECEIVED.value,
                                Integer,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            func.cast(
                                PurchaseOrder.status == PurchaseOrderStatus.CANCELLED.value,
                                Integer,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(func.sum(func.cast(overdue, Integer)), 0),
                    # The status test is cast to an integer rather than to a
                    # decimal: PostgreSQL has no boolean-to-numeric cast, and an
                    # integer times the order's own total is the same money.
                    func.coalesce(
                        func.sum(
                            func.cast(counted, Integer) * PurchaseOrder.total_amount
                        ),
                        0.0,
                    ),
                    func.coalesce(
                        func.sum(
                            func.cast(received, Integer) * PurchaseOrder.total_amount
                        ),
                        0.0,
                    ),
                )
            )
        ).one()
        (
            total_orders,
            draft_orders,
            open_orders,
            received_orders,
            cancelled_orders,
            overdue_orders,
            committed,
            received_value,
        ) = row

        return PurchaseOrderSummary(
            total_orders=int(total_orders or 0),
            draft_orders=int(draft_orders or 0),
            open_orders=int(open_orders or 0),
            received_orders=int(received_orders or 0),
            cancelled_orders=int(cancelled_orders or 0),
            overdue_orders=int(overdue_orders or 0),
            total_committed=round(float(committed or 0.0), 2),
            total_received_value=round(float(received_value or 0.0), 2),
        )

    # --- creation ------------------------------------------------------------

    async def create_purchase_order(
        self, data: PurchaseOrderCreate, created_by_id: UUID | str | None = None
    ) -> PurchaseOrder:
        """Raise a purchase order with its initial lines.

        The header, every line and the money totals land in one transaction:
        either the whole order exists or none of it does.
        """
        supplier = await self.suppliers.assert_orderable(data.supplier_id)

        order_date = data.order_date or today()
        expected = data.expected_delivery_date
        if expected is None and supplier.lead_time_days:
            # A suggestion, not a promise: the supplier's quoted lead time seeds
            # the date, and the order can still be given a different one.
            expected = order_date + timedelta(days=int(supplier.lead_time_days))

        parts = await self._require_parts([item.part_id for item in data.items])

        po = PurchaseOrder(
            po_number=self._new_po_number(),
            supplier_id=str(supplier.id),
            status=PurchaseOrderStatus.DRAFT.value,
            order_date=order_date,
            expected_delivery_date=expected,
            tax_amount=round_money(data.tax_amount),
            shipping_amount=round_money(data.shipping_amount),
            currency=data.currency,
            notes=data.notes,
            internal_notes=data.internal_notes,
            created_by_id=str(created_by_id) if created_by_id else None,
            # Seeding the collection through the constructor loads it up front;
            # appending afterwards would touch the still-unloaded relationship
            # and trigger lazy IO outside the async greenlet context.
            items=[
                self._build_item(item_data, index, parts)
                for index, item_data in enumerate(data.items, start=1)
            ],
        )
        self.db.add(po)
        await self.db.flush()

        self.recalculate(po)
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError(f"Purchase order {po.po_number} conflicts with an existing order")
        logger.info("Purchase order raised: %s with %s line(s)", po.po_number, len(po.items))
        return await self.get_by_id(po.id)

    async def create_from_low_stock(
        self, data: PurchaseOrderFromLowStock, created_by_id: UUID | str | None = None
    ) -> PurchaseOrder:
        """Raise a draft order covering the parts that are at or below reorder level.

        The low-stock test is the same ``quantity_on_hand <= reorder_level`` the
        buying list uses, so the order and the alert can never disagree. Quantity
        is the shortage multiplied by ``shortage_multiplier`` and rounded up to a
        whole unit — a supplier ships whole parts — with a floor of one unit,
        because a part that has tripped the reorder point is by definition due to
        be bought and ordering zero of it would quietly drop it from the order.
        """
        supplier = await self.suppliers.assert_orderable(data.supplier_id)

        stmt = select(Part).where(Part.quantity_on_hand <= Part.reorder_level)
        if not data.include_discontinued:
            stmt = stmt.where(Part.status == PartStatus.ACTIVE.value)
        if data.part_ids:
            stmt = stmt.where(Part.id.in_([str(pid) for pid in data.part_ids]))
        stmt = stmt.order_by(Part.name.asc())

        parts = list((await self.db.execute(stmt)).scalars().all())
        if not parts:
            raise BusinessRuleError(
                "No parts are at or below their reorder level, so there is nothing to order"
            )

        order_date = today()
        expected = data.expected_delivery_date
        if expected is None and supplier.lead_time_days:
            expected = order_date + timedelta(days=int(supplier.lead_time_days))

        items = []
        for part in parts:
            shortage = max(float(part.reorder_level) - float(part.quantity_on_hand), 0.0)
            quantity = max(
                _round_qty(math.ceil(shortage * data.shortage_multiplier)),
                1.0,
            )
            items.append(
                PurchaseOrderItem(
                    part_id=str(part.id),
                    line_number=len(items) + 1,
                    part_number=part.part_number,
                    part_name=part.name,
                    quantity_ordered=quantity,
                    quantity_received=0.0,
                    # The catalog cost is only a starting price; the real invoice
                    # price is captured when the line is received.
                    unit_cost=round_money(part.unit_cost),
                )
            )

        po = PurchaseOrder(
            po_number=self._new_po_number(),
            supplier_id=str(supplier.id),
            status=PurchaseOrderStatus.DRAFT.value,
            order_date=order_date,
            expected_delivery_date=expected,
            tax_amount=round_money(data.tax_amount),
            shipping_amount=round_money(data.shipping_amount),
            notes=data.notes or "Raised from the low-stock list",
            created_by_id=str(created_by_id) if created_by_id else None,
            items=items,
        )
        self.db.add(po)
        await self.db.flush()

        self.recalculate(po)
        await self.db.commit()
        logger.info(
            "Purchase order %s raised from low stock with %s line(s)", po.po_number, len(po.items)
        )
        return await self.get_by_id(po.id)

    # --- draft edits ---------------------------------------------------------

    async def update_purchase_order(
        self, purchase_order_id: UUID | str, data: PurchaseOrderUpdate
    ) -> PurchaseOrder:
        """Edit a draft order's header, or replace its lines wholesale."""
        po = await self.get_by_id(purchase_order_id)
        self._assert_editable(po)

        changes = data.model_dump(exclude_unset=True, exclude={"items"})
        for field, value in changes.items():
            setattr(po, field, value)

        if data.items is not None:
            # Replacing the collection drops the old lines and adds the new ones.
            # Safe only because a draft has never been sent, so nothing outside
            # the shop has seen the previous lines.
            parts = await self._require_parts([item.part_id for item in data.items])
            po.items.clear()
            await self.db.flush()
            for index, item_data in enumerate(data.items, start=1):
                po.items.append(self._build_item(item_data, index, parts))

        self.recalculate(po)
        return await self._save(po)

    async def add_item(
        self, purchase_order_id: UUID | str, item_data: PurchaseOrderItemCreate
    ) -> PurchaseOrder:
        """Append a line to a draft order."""
        po = await self.get_by_id(purchase_order_id)
        self._assert_editable(po)

        if any(str(i.part_id) == str(item_data.part_id) for i in po.items):
            raise ConflictError(
                f"Part {item_data.part_id} is already on {po.po_number}; edit that "
                "line instead of adding a second one for the same part"
            )

        parts = await self._require_parts([item_data.part_id])
        po.items.append(
            self._build_item(item_data, len(po.items) + 1, parts)
        )
        return await self._save(po)

    async def update_item(
        self,
        purchase_order_id: UUID | str,
        item_id: UUID | str,
        data: PurchaseOrderItemUpdate,
    ) -> PurchaseOrder:
        """Edit one line of a draft order."""
        po = await self.get_by_id(purchase_order_id)
        self._assert_editable(po)

        item = self._find_item(po, item_id)
        changes = data.model_dump(exclude_unset=True)
        for field, value in changes.items():
            setattr(item, field, value)

        return await self._save(po)

    async def remove_item(self, purchase_order_id: UUID | str, item_id: UUID | str) -> PurchaseOrder:
        """Drop a line from a draft order.

        The last line cannot be removed: an order with nothing on it commits the
        shop to nothing, so it should be cancelled or deleted instead.
        """
        po = await self.get_by_id(purchase_order_id)
        self._assert_editable(po)

        item = self._find_item(po, item_id)
        if len(po.items) == 1:
            raise BusinessRuleError(
                f"Cannot remove the last line of {po.po_number}; cancel or delete the "
                "order instead"
            )

        po.items.remove(item)
        # Renumber so the remaining lines stay 1..n with no gap.
        for position, remaining in enumerate(po.items, start=1):
            remaining.line_number = position

        return await self._save(po)

    # --- status --------------------------------------------------------------

    async def send(self, purchase_order_id: UUID | str) -> PurchaseOrder:
        """Send a draft order to the supplier."""
        po = await self.get_by_id(purchase_order_id)
        self._transition(po, PurchaseOrderStatus.SENT.value)
        self._assert_sendable(po)
        po.status = PurchaseOrderStatus.SENT.value
        po.sent_at = _utcnow()
        return await self._save(po)

    async def cancel(self, purchase_order_id: UUID | str, reason: str | None = None) -> PurchaseOrder:
        """Call a purchase order off.

        Stock already received against a cancelled order stays on the ledger: the
        parts are on the shelf and the supplier has been paid for them. Cancelling
        the paperwork does not un-buy goods.
        """
        po = await self.get_by_id(purchase_order_id)
        self._transition(po, PurchaseOrderStatus.CANCELLED.value)
        po.status = PurchaseOrderStatus.CANCELLED.value
        po.cancelled_at = _utcnow()
        if reason:
            po.cancel_reason = reason
        return await self._save(po)

    async def update_status(
        self,
        purchase_order_id: UUID | str,
        new_status: str,
        reason: str | None = None,
    ) -> PurchaseOrder:
        """Transition a purchase order's status, enforcing the state machine.

        Sending and cancelling go through :meth:`send` and :meth:`cancel` so the
        milestone stamps and the send guard always apply.

        ``PARTIALLY_RECEIVED`` and ``RECEIVED`` are deliberately not reachable
        here: an order that claims a delivery it never booked in would disagree
        with its own lines and with the stock ledger. They are reached only
        through :meth:`receive`, which files the inventory receipts that make
        them true.
        """
        try:
            new_status = PurchaseOrderStatus(str(new_status).upper()).value
        except (ValueError, AttributeError):
            allowed = ", ".join(s.value for s in PurchaseOrderStatus)
            raise BusinessRuleError(
                f"Invalid purchase order status: {new_status}. Allowed: {allowed}"
            )

        if new_status in (
            PurchaseOrderStatus.PARTIALLY_RECEIVED.value,
            PurchaseOrderStatus.RECEIVED.value,
        ):
            raise BusinessRuleError(
                f"{new_status} is set by receiving a delivery, not by a status change: "
                f"POST /api/v1/purchase_orders/{purchase_order_id}/receive"
            )

        if new_status == PurchaseOrderStatus.SENT.value:
            return await self.send(purchase_order_id)
        if new_status == PurchaseOrderStatus.CANCELLED.value:
            return await self.cancel(purchase_order_id, reason)

        po = await self.get_by_id(purchase_order_id)
        self._transition(po, new_status)
        po.status = new_status
        return await self._save(po)

    # --- receiving -----------------------------------------------------------

    async def receive(
        self,
        purchase_order_id: UUID | str,
        data: ReceiveRequest,
        performed_by_id: UUID | str | None = None,
    ) -> ReceiveResult:
        """Book a delivery in against a sent order.

        Each received line is filed as an inventory ``RECEIPT`` carrying the PO
        number as its reference, so "where did this stock come from" is answered
        by the ledger, not by memory. A line may quote a different ``unit_cost``
        from the one ordered: the supplier's invoice is the truth about what was
        paid, and it is captured on the receipt itself rather than rewriting
        catalog pricing.

        The whole delivery is one database transaction, so a failure part-way
        through leaves the stock balance exactly as it was rather than booking in
        half an order.
        """
        po = await self.get_by_id(purchase_order_id)
        self._assert_receivable(po)

        # Everything is validated before a single row is written, so the common
        # rejections (unknown line, over-delivery) never leave stock half moved.
        planned: list[tuple[PurchaseOrderItem, ReceiveLine, float]] = []
        for line in data.items:
            item = self._find_item(po, line.item_id)
            if item.is_fully_received:
                raise BusinessRuleError(
                    f"Line {item.part_number} has already been fully received and "
                    "cannot receive more"
                )
            quantity = _round_qty(line.quantity)
            received = _round_qty(item.quantity_received)
            ordered = _round_qty(item.quantity_ordered)
            if _round_qty(received + quantity) > ordered:
                raise BusinessRuleError(
                    f"Cannot receive {quantity} of {item.part_number}: only "
                    f"{round_money(ordered - received)} unit(s) are still outstanding "
                    f"on {po.po_number}. Raise a new line or adjust the order instead of "
                    "over-receiving"
                )
            planned.append((item, line, quantity))

        receipts: list[ReceiptReference] = []
        try:
            for item, line, quantity in planned:
                unit_cost = (
                    round_money(line.unit_cost) if line.unit_cost is not None else item.unit_cost
                )
                transaction = await self.inventory.record_transaction(
                    InventoryTransactionCreate(
                        part_id=str(item.part_id),
                        transaction_type=InventoryTransactionType.RECEIPT.value,
                        quantity=quantity,
                        unit_cost=unit_cost,
                        # The PO number is the reference that ties the delivery back
                        # to the order that caused it.
                        reference=po.po_number,
                    ),
                    performed_by_id=performed_by_id,
                    # One commit for the whole delivery: see the module docstring.
                    commit=False,
                )
                item.quantity_received = _round_qty(item.quantity_received + quantity)
                receipts.append(
                    ReceiptReference(
                        item_id=item.id,
                        part_id=item.part_id,
                        part_number=item.part_number,
                        quantity=quantity,
                        unit_cost=unit_cost,
                        transaction_id=transaction.id,
                    )
                )

            if data.notes:
                po.internal_notes = (
                    f"{po.internal_notes}\n{data.notes}" if po.internal_notes else data.notes
                )

            # The order's status follows what has actually arrived: fully received
            # only when every line is complete, so an order can never close itself
            # by having all of one line delivered.
            if po.is_fully_received:
                po.status = PurchaseOrderStatus.RECEIVED.value
                if po.received_at is None:
                    po.received_at = _utcnow()
            else:
                po.status = PurchaseOrderStatus.PARTIALLY_RECEIVED.value

            await self.db.commit()
        except Exception:
            # Rolls back the receipts too, so the balance and the order agree.
            await self.db.rollback()
            logger.warning("Receiving against %s failed and was rolled back", po.po_number)
            raise

        received_units = _round_qty(sum(receipt.quantity for receipt in receipts))
        logger.info(
            "Received %s unit(s) against %s -> %s", received_units, po.po_number, po.status
        )
        return ReceiveResult(
            purchase_order=await self.get_by_id(po.id),
            received_units=received_units,
            receipts=receipts,
        )

    # --- deletion ------------------------------------------------------------

    async def delete_purchase_order(self, purchase_order_id: UUID | str) -> None:
        """Delete a draft order.

        A sent order is a commitment to a supplier, and a received one is a
        receipt the shop paid against; neither can be deleted, only cancelled.
        """
        po = await self.get_by_id(purchase_order_id)
        if po.status != PurchaseOrderStatus.DRAFT.value:
            raise BusinessRuleError(
                f"Purchase order {po.po_number} is {po.status}; only a DRAFT order can be "
                "deleted, cancel it instead"
            )
        await self.db.delete(po)
        await self.db.commit()
        logger.info("Purchase order deleted: %s", po.po_number)

    # --- internals -----------------------------------------------------------

    @staticmethod
    def recalculate(po: PurchaseOrder) -> None:
        """Recompute the order's cached money totals from its lines.

        The subtotal is the line arithmetic; tax and shipping are the order's own
        charges. The total is derived from those two rather than accepted from a
        caller, so the header can never disagree with its own lines.
        """
        po.subtotal = round_money(sum(item.line_total for item in po.items))
        po.total_amount = round_money(
            float(po.subtotal) + float(po.tax_amount) + float(po.shipping_amount)
        )

    async def _save(self, po: PurchaseOrder) -> PurchaseOrder:
        """Recalculate and commit a purchase order, then re-read it."""
        self.recalculate(po)
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("Purchase order update conflicts with another order")
        return await self.get_by_id(po.id)

    @staticmethod
    def _new_po_number() -> str:
        """Generate a human-readable, collision-resistant purchase-order number.

        A date prefix makes the number recognisable when a delivery note turns
        up; the random suffix keeps concurrent creates from colliding, and the
        column is unique so any residual clash surfaces as a 409 rather than
        silent duplication.
        """
        stamp = datetime.now(UTC).strftime("%Y%m")
        suffix = uuid_module.uuid4().hex[:6].upper()
        return f"PO-{stamp}-{suffix}"

    @staticmethod
    def _build_item(
        item_data: PurchaseOrderItemCreate,
        line_number: int,
        parts: dict[str, Part],
    ) -> PurchaseOrderItem:
        """Build one order line (not yet added to the session).

        The part's number and name are copied onto the line because a supplier's
        paperwork quotes them: if the catalog is later renamed, the order must
        still read the way it was written.
        """
        part = parts[str(item_data.part_id)]
        return PurchaseOrderItem(
            part_id=str(part.id),
            line_number=line_number,
            part_number=part.part_number,
            part_name=part.name,
            quantity_ordered=_round_qty(item_data.quantity_ordered),
            quantity_received=0.0,
            unit_cost=round_money(item_data.unit_cost),
            notes=item_data.notes,
        )

    async def _require_parts(self, part_ids: list[UUID | str]) -> dict[str, Part]:
        """Load the parts an order's lines refer to, keyed by id.

        Every referenced part must exist, and a discontinued part cannot be
        ordered: the inventory service refuses to restock one, so an order for it
        could never be received.
        """
        wanted = [str(part_id) for part_id in part_ids]
        result = await self.db.execute(select(Part).where(Part.id.in_(wanted)))
        parts = {str(part.id): part for part in result.scalars().all()}

        missing = [part_id for part_id in wanted if part_id not in parts]
        if missing:
            raise NotFoundError(f"Part {missing[0]} not found")

        discontinued = [p.part_number for p in parts.values() if p.status != PartStatus.ACTIVE.value]
        if discontinued:
            raise BusinessRuleError(
                f"Part {discontinued[0]} is DISCONTINUED and cannot be ordered; "
                "reactivate it first"
            )
        return parts

    @staticmethod
    def _find_item(po: PurchaseOrder, item_id: UUID | str) -> PurchaseOrderItem:
        item = next((i for i in po.items if str(i.id) == str(item_id)), None)
        if not item:
            raise NotFoundError(f"Line {item_id} not found on purchase order {po.po_number}")
        return item

    @staticmethod
    def _assert_editable(po: PurchaseOrder) -> None:
        if not po.is_editable:
            raise BusinessRuleError(
                f"Purchase order {po.po_number} is {po.status}; only a DRAFT order can be "
                "edited. Cancel it and raise a new one"
            )

    @staticmethod
    def _assert_receivable(po: PurchaseOrder) -> None:
        if po.status not in (
            PurchaseOrderStatus.SENT.value,
            PurchaseOrderStatus.PARTIALLY_RECEIVED.value,
        ):
            raise BusinessRuleError(
                f"Purchase order {po.po_number} is {po.status}; only a SENT order can "
                "receive a delivery. Send it first"
            )

    @staticmethod
    def _assert_sendable(po: PurchaseOrder) -> None:
        """An order must have lines and a live supplier before it goes out.

        A supplier that was retired after the order was drafted is checked here
        as well: sending it would commit the shop to buying from a business it has
        stopped trading with.
        """
        if not po.items:
            raise BusinessRuleError("Cannot send a purchase order with no line items")
        if po.supplier is not None and not po.supplier.is_active:
            raise BusinessRuleError(
                f"Supplier {po.supplier.name} is {po.supplier.status}; reactivate it "
                "before sending this order"
            )

    @staticmethod
    def _transition(po: PurchaseOrder, new_status: str) -> None:
        """Apply a status change, enforcing the purchase-order state machine."""
        if po.status == new_status:
            return
        if not is_transition_allowed(po.status, new_status):
            allowed = PURCHASE_ORDER_STATUS_TRANSITIONS.get(po.status, [])
            raise BusinessRuleError(
                f"Cannot transition purchase order status from '{po.status}' to "
                f"'{new_status}'. Allowed: {allowed or 'none (terminal state)'}"
            )


__all__ = ["OPEN_PURCHASE_ORDER_STATUSES", "PurchaseOrderService"]
