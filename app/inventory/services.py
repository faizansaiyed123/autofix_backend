"""Inventory business logic.

Owns every movement of stock and the balance it produces. The rule the rest of
the system leans on: a part's ``quantity_on_hand`` is never assigned by a
caller, only derived. You say "four arrived" or "one went out on RO-123", and
this service decides what the balance becomes and files the row that proves it.

Two guards make the ledger trustworthy:

* the part row is locked ``FOR UPDATE`` while the movement is applied, so two
  parts staff issuing parts at the same moment cannot both read the same
  balance and lose one of the issues;
* stock cannot go negative — you cannot issue a part the shop does not have.
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import BusinessRuleError, NotFoundError
from app.inventory.models import (
    InventoryTransaction,
    InventoryTransactionType,
    direction_for,
)
from app.inventory.schemas import (
    InventoryTransactionCreate,
    LowStockAlert,
    StockLevel,
    StockSummary,
)
from app.parts.models import Part, PartStatus

logger = logging.getLogger("autofix.inventory.services")

# Two decimal places is the precision the NUMERIC columns keep, so quantities
# are rounded before they are compared or stored. Otherwise 0.1 + 0.2 would
# leave a balance of 0.30000000000000004 that disagrees with its own ledger.
QUANTITY_PRECISION = 2


def _round(value: float) -> float:
    return round(float(value), QUANTITY_PRECISION)


class InventoryService:
    """Service for stock movements, stock levels and low-stock alerts."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # --- reads --------------------------------------------------------------

    async def get_transaction(self, transaction_id: UUID | str) -> InventoryTransaction:
        """Get one recorded movement."""
        result = await self.db.execute(
            select(InventoryTransaction)
            .where(InventoryTransaction.id == str(transaction_id))
            .execution_options(populate_existing=True)
        )
        transaction = result.scalar_one_or_none()
        if not transaction:
            raise NotFoundError(f"Inventory transaction with id {transaction_id} not found")
        return transaction

    async def list_transactions(
        self,
        *,
        page: int = 1,
        size: int = 20,
        part_id: UUID | str | None = None,
        transaction_type: str | None = None,
        repair_order_id: UUID | str | None = None,
        performed_by_id: UUID | str | None = None,
        reference: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> tuple[list[InventoryTransaction], int]:
        """List stock movements, newest first, with filtering and pagination."""
        stmt = select(InventoryTransaction)
        count_stmt = select(func.count()).select_from(InventoryTransaction)

        filters = []
        if part_id:
            filters.append(InventoryTransaction.part_id == str(part_id))
        if transaction_type:
            try:
                value = InventoryTransactionType(str(transaction_type).upper()).value
            except ValueError:
                allowed = ", ".join(t.value for t in InventoryTransactionType)
                raise BusinessRuleError(
                    f"Invalid inventory transaction type '{transaction_type}'. "
                    f"Allowed: {allowed}"
                )
            filters.append(InventoryTransaction.transaction_type == value)
        if repair_order_id:
            filters.append(InventoryTransaction.repair_order_id == str(repair_order_id))
        if performed_by_id:
            filters.append(InventoryTransaction.performed_by_id == str(performed_by_id))
        if reference:
            filters.append(func.lower(InventoryTransaction.reference) == str(reference).lower())
        if start_date:
            filters.append(InventoryTransaction.created_at >= start_date)
        if end_date:
            filters.append(InventoryTransaction.created_at <= end_date)

        for condition in filters:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        total = (await self.db.execute(count_stmt)).scalar_one()

        offset = (page - 1) * size
        stmt = (
            stmt.order_by(InventoryTransaction.created_at.desc(), InventoryTransaction.id.desc())
            .offset(offset)
            .limit(size)
        )
        transactions = list((await self.db.execute(stmt)).scalars().all())
        return transactions, total

    async def part_history(
        self, part_id: UUID | str, *, limit: int = 50
    ) -> list[InventoryTransaction]:
        """The movements of one part, newest first."""
        part = await self._require_part(part_id)
        result = await self.db.execute(
            select(InventoryTransaction)
            .where(InventoryTransaction.part_id == str(part.id))
            .order_by(InventoryTransaction.created_at.desc(), InventoryTransaction.id.desc())
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def low_stock_parts(
        self, *, include_discontinued: bool = False
    ) -> list[LowStockAlert]:
        """Parts at or below their reorder level.

        This is the shop's buying list. It is derived rather than stored because
        a stored flag goes stale the moment stock moves: the only honest test is
        ``quantity_on_hand <= reorder_level`` against the current balance.
        """
        stmt = select(Part).where(Part.quantity_on_hand <= Part.reorder_level)
        if not include_discontinued:
            stmt = stmt.where(Part.status == PartStatus.ACTIVE.value)

        # Most urgent first: nothing on the shelf, then furthest below the line.
        stmt = stmt.order_by(Part.quantity_on_hand.asc(), Part.name.asc())
        parts = list((await self.db.execute(stmt)).scalars().all())

        alerts = []
        for part in parts:
            on_hand = float(part.quantity_on_hand)
            reorder = float(part.reorder_level)
            alerts.append(
                LowStockAlert(
                    part_id=part.id,
                    part_number=part.part_number,
                    name=part.name,
                    category=part.category,
                    brand=part.brand,
                    location=part.location,
                    quantity_on_hand=on_hand,
                    reorder_level=reorder,
                    # How much would be needed to get back above the line. A part
                    # with no reorder level set is short by nothing: it has not
                    # been flagged for reordering at all.
                    shortage=_round(max(reorder - on_hand, 0.0)),
                    stock_status=part.stock_status,
                )
            )
        return alerts

    async def stock_levels(
        self, *, category: str | None = None, low_stock_only: bool = False
    ) -> list[StockLevel]:
        """Balance and valuation for every part."""
        stmt = select(Part)
        if category:
            stmt = stmt.where(func.lower(Part.category) == str(category).strip().lower())
        if low_stock_only:
            stmt = stmt.where(Part.quantity_on_hand <= Part.reorder_level)
        stmt = stmt.order_by(Part.name.asc())

        parts = list((await self.db.execute(stmt)).scalars().all())
        return [
            StockLevel(
                part_id=part.id,
                part_number=part.part_number,
                name=part.name,
                category=part.category,
                location=part.location,
                quantity_on_hand=float(part.quantity_on_hand),
                reorder_level=float(part.reorder_level),
                unit_cost=float(part.unit_cost),
                stock_value=part.stock_value,
                stock_status=part.stock_status,
                is_low_stock=part.is_low_stock,
            )
            for part in parts
        ]

    async def stock_summary(self) -> StockSummary:
        """Headline numbers across the shelf, for a dashboard tile."""
        result = (
            await self.db.execute(
                select(
                    func.count(Part.id),
                    func.coalesce(func.sum(Part.quantity_on_hand), 0.0),
                    func.coalesce(
                        func.sum(Part.quantity_on_hand * Part.unit_cost), 0.0
                    ),
                    func.coalesce(
                        func.sum(
                            func.cast(
                                Part.quantity_on_hand <= Part.reorder_level,
                                Integer,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(func.cast(Part.quantity_on_hand <= 0, Integer)),
                        0,
                    ),
                ).where(Part.status == PartStatus.ACTIVE.value)
            )
        ).one()
        total_parts, total_units, total_value, low_stock, out_of_stock = result

        return StockSummary(
            total_parts=int(total_parts or 0),
            total_units=_round(total_units or 0.0),
            total_stock_value=_round(total_value or 0.0),
            low_stock_count=int(low_stock or 0),
            out_of_stock_count=int(out_of_stock or 0),
        )

    # --- writes -------------------------------------------------------------

    async def record_transaction(
        self,
        data: InventoryTransactionCreate,
        performed_by_id: UUID | str | None = None,
        *,
        commit: bool = True,
    ) -> InventoryTransaction:
        """Record a stock movement and move the part's balance with it.

        ``commit=False`` leaves the movement written but uncommitted, so a caller
        filing several movements as one logical event — booking in a supplier
        delivery line by line — can land them all together or not at all. The
        part's row lock is held until that caller's own commit, which is exactly
        what makes the batch atomic.
        """
        part = await self._lock_part(data.part_id)

        tx_type = InventoryTransactionType(str(data.transaction_type).upper()).value
        stated_direction = (
            str(data.direction).upper() if data.direction is not None else None
        )
        magnitude = _round(data.quantity)

        try:
            direction = direction_for(tx_type, stated_direction)
        except ValueError as exc:
            raise BusinessRuleError(str(exc))

        quantity = _round(magnitude * direction)

        if tx_type == InventoryTransactionType.SCRAP.value and not data.reason:
            raise BusinessRuleError("a SCRAP must say why the stock was written off")
        if tx_type == InventoryTransactionType.ADJUSTMENT.value and not data.reason:
            raise BusinessRuleError(
                "an ADJUSTMENT must say what was found at the stock take"
            )
        if (
            tx_type == InventoryTransactionType.RECEIPT.value
            and part.status == PartStatus.DISCONTINUED.value
        ):
            raise BusinessRuleError(
                f"Part {part.part_number} is DISCONTINUED and cannot be restocked; "
                "reactivate it first or raise a purchase order for it"
            )

        before = _round(part.quantity_on_hand)
        after = _round(before + quantity)
        if after < 0:
            raise BusinessRuleError(
                f"Cannot {tx_type.lower()} {magnitude} unit(s) of "
                f"{part.part_number}: only {before} on hand. Stock cannot go negative"
            )

        # A receipt's unit cost is what that stock actually cost, captured on the
        # row. It defaults to today's catalog cost so a simple delivery does not
        # have to repeat the price, but it never changes the catalog: re-pricing
        # is a catalog decision, not a side effect of a booking-in.
        unit_cost = (
            _round(data.unit_cost)
            if data.unit_cost is not None
            else _round(part.unit_cost)
        )

        transaction = InventoryTransaction(
            part_id=str(part.id),
            transaction_type=tx_type,
            quantity=quantity,
            quantity_before=before,
            quantity_after=after,
            unit_cost=unit_cost,
            repair_order_id=str(data.repair_order_id) if data.repair_order_id else None,
            reference=data.reference,
            performed_by_id=str(performed_by_id) if performed_by_id else None,
            reason=data.reason,
            from_location=data.from_location,
            to_location=data.to_location,
        )
        part.quantity_on_hand = after

        self.db.add(transaction)
        if commit:
            await self.db.commit()
        else:
            # Flushed but not committed: the balance change and the ledger row are
            # in the database, and the caller's transaction decides whether they
            # survive.
            await self.db.flush()
        await self.db.refresh(transaction)

        logger.info(
            "Inventory %s: part %s %+s (balance %s -> %s)",
            tx_type,
            part.part_number,
            quantity,
            before,
            after,
        )
        return transaction

    # --- internals ----------------------------------------------------------

    async def _require_part(self, part_id: UUID | str) -> Part:
        result = await self.db.execute(
            select(Part).where(Part.id == str(part_id)).execution_options(populate_existing=True)
        )
        part = result.scalar_one_or_none()
        if not part:
            raise NotFoundError(f"Part with id {part_id} not found")
        return part

    async def _lock_part(self, part_id: UUID | str) -> Part:
        """Read a part with a row lock held for the rest of the transaction.

        Two concurrent issues would otherwise both read the same balance and the
        second write would overwrite the first, quietly losing stock. The lock
        serialises them, so the second issue sees the first one's result.
        """
        result = await self.db.execute(
            select(Part)
            .where(Part.id == str(part_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        part = result.scalar_one_or_none()
        if not part:
            raise NotFoundError(f"Part with id {part_id} not found")
        return part


__all__ = ["InventoryService"]
