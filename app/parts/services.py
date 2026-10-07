"""Parts catalog business logic.

Owns the catalog itself: adding, editing, retiring and removing lines. It is
deliberately not the place stock levels change — see
:mod:`app.inventory.services`, which owns every movement and keeps the balance on
a part in step with its ledger.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.inventory.models import InventoryTransaction
from app.parts.models import Part, PartStatus
from app.parts.schemas import PartCreate, PartUpdate

logger = logging.getLogger("autofix.parts.services")


class PartService:
    """Service for the parts catalog."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # --- reads --------------------------------------------------------------

    async def get_by_id(self, part_id: UUID | str) -> Part:
        """Get a catalog line by ID."""
        result = await self.db.execute(
            select(Part).where(Part.id == str(part_id)).execution_options(populate_existing=True)
        )
        part = result.scalar_one_or_none()
        if not part:
            raise NotFoundError(f"Part with id {part_id} not found")
        return part

    async def get_by_part_number(self, part_number: str) -> Part:
        """Get a catalog line by its manufacturer part number."""
        result = await self.db.execute(
            select(Part)
            .where(func.upper(Part.part_number) == str(part_number).strip().upper())
            .execution_options(populate_existing=True)
        )
        part = result.scalar_one_or_none()
        if not part:
            raise NotFoundError(f"Part with part number {part_number} not found")
        return part

    async def list_parts(
        self,
        *,
        page: int = 1,
        size: int = 20,
        category: str | None = None,
        status: str | None = None,
        search: str | None = None,
        low_stock: bool | None = None,
        in_stock: bool | None = None,
    ) -> tuple[list[Part], int]:
        """List catalog lines with filtering and pagination.

        ``search`` matches part number, SKU, name or brand, which is how anyone
        actually looks for a part: they have a number off a quote, or a
        half-remembered description, not a category.
        """
        stmt = select(Part)
        count_stmt = select(func.count()).select_from(Part)

        filters = []
        if category:
            filters.append(func.lower(Part.category) == str(category).strip().lower())
        if status:
            try:
                filters.append(Part.status == PartStatus(str(status).upper()).value)
            except ValueError:
                # An unknown status matches nothing rather than everything: a
                # mistyped filter must never silently return the whole catalog.
                filters.append(Part.id.is_(None))
        if search:
            term = f"%{str(search).strip().lower()}%"
            filters.append(
                or_(
                    func.lower(Part.part_number).like(term),
                    func.lower(func.coalesce(Part.sku, "")).like(term),
                    func.lower(Part.name).like(term),
                    func.lower(func.coalesce(Part.brand, "")).like(term),
                )
            )
        if low_stock is not None:
            # At or below the reorder level — the same test the alert list uses.
            if low_stock:
                filters.append(Part.quantity_on_hand <= Part.reorder_level)
            else:
                filters.append(Part.quantity_on_hand > Part.reorder_level)
        if in_stock is not None:
            filters.append(
                Part.quantity_on_hand > 0 if in_stock else Part.quantity_on_hand <= 0
            )

        for condition in filters:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        total = (await self.db.execute(count_stmt)).scalar_one()

        offset = (page - 1) * size
        stmt = (
            stmt.order_by(Part.name.asc(), Part.part_number.asc())
            .offset(offset)
            .limit(size)
        )
        parts = list((await self.db.execute(stmt)).scalars().all())
        return parts, total

    async def list_categories(self) -> list[str]:
        """Every category currently in use, alphabetically."""
        result = await self.db.execute(
            select(Part.category).distinct().order_by(Part.category.asc())
        )
        return list(result.scalars().all())

    # --- writes -------------------------------------------------------------

    async def create_part(self, data: PartCreate) -> Part:
        """Add a catalog line. The part starts with no stock."""
        part = Part(
            part_number=data.part_number,
            sku=data.sku,
            name=data.name,
            description=data.description,
            category=data.category,
            brand=data.brand,
            location=data.location,
            unit_cost=float(data.unit_cost),
            unit_price=float(data.unit_price),
            quantity_on_hand=0.0,
            reorder_level=float(data.reorder_level),
            status=PartStatus.ACTIVE.value,
        )
        self.db.add(part)
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            duplicate = "sku" if data.sku else "part number"
            raise ConflictError(
                f"A part with this {duplicate} already exists ({data.sku or data.part_number})"
            )
        await self.db.refresh(part)
        logger.info("Part added to catalog: %s (%s)", part.name, part.part_number)
        return part

    async def update_part(self, part_id: UUID | str, update_data: PartUpdate) -> Part:
        """Edit a catalog line's details, pricing or status."""
        part = await self.get_by_id(part_id)

        changes = update_data.model_dump(exclude_unset=True)
        if not changes:
            return part

        for field, value in changes.items():
            setattr(part, field, value)

        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("Part update conflicts with an existing catalog line")
        await self.db.refresh(part)
        return part

    async def delete_part(self, part_id: UUID | str) -> None:
        """Delete a retired, empty catalog line.

        A part with stock cannot be deleted (the shop still owes itself the
        units), and neither can one that has ever moved: deleting it would take
        its ledger with it. Retiring it with ``status=DISCONTINUED`` is the way
        to stop trading something while keeping the record.
        """
        part = await self.get_by_id(part_id)

        if part.status != PartStatus.DISCONTINUED.value:
            raise BusinessRuleError(
                f"Part {part.part_number} is {part.status}; retire it "
                "(status=DISCONTINUED) before deleting it"
            )

        if float(part.quantity_on_hand) != 0:
            raise BusinessRuleError(
                f"Part {part.part_number} still has {part.quantity_on_hand} unit(s) "
                "on hand; stock it down to zero before deleting it"
            )

        movements = (
            await self.db.execute(
                select(func.count())
                .select_from(InventoryTransaction)
                .where(InventoryTransaction.part_id == str(part.id))
            )
        ).scalar_one()
        if movements:
            raise BusinessRuleError(
                f"Part {part.part_number} has {movements} inventory transaction(s); "
                "its history cannot be deleted. Retire it instead"
            )

        await self.db.delete(part)
        await self.db.commit()
        logger.info("Part deleted from catalog: %s", part_id)


__all__ = ["PartService"]
