"""Supplier business logic.

Owns the counterparty side of buying: the list of suppliers, their contact and
delivery details, and which of them the shop still trades with. Purchase orders
raised against a supplier live in :mod:`app.purchase_orders.services`.

Names are compared case-insensitively so "Bosch Parts" and "bosch parts" cannot
become two suppliers and split the shop's order history between them.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.purchase_orders.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
)
from app.suppliers.models import Supplier, SupplierStatus
from app.suppliers.schemas import SupplierCreate, SupplierSummary, SupplierUpdate

logger = logging.getLogger("autofix.suppliers.services")

# Order statuses that still owe the shop a delivery: something to chase, or
# stock still on its way.
OPEN_PURCHASE_ORDER_STATUSES: frozenset[str] = frozenset(
    {
        PurchaseOrderStatus.DRAFT.value,
        PurchaseOrderStatus.SENT.value,
        PurchaseOrderStatus.PARTIALLY_RECEIVED.value,
    }
)

# Statuses where goods have actually been counted into stock, so money was spent
# and units arrived. Spend and units are counted from these orders only, because
# a draft or cancelled order never cost the shop anything.
RECEIVED_PURCHASE_ORDER_STATUSES: frozenset[str] = frozenset(
    {
        PurchaseOrderStatus.RECEIVED.value,
        PurchaseOrderStatus.PARTIALLY_RECEIVED.value,
    }
)


class SupplierService:
    """Service for the supplier list."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # --- reads --------------------------------------------------------------

    async def get_by_id(self, supplier_id: UUID | str) -> Supplier:
        """Get a supplier by ID."""
        result = await self.db.execute(
            select(Supplier)
            .where(Supplier.id == str(supplier_id))
            .execution_options(populate_existing=True)
        )
        supplier = result.scalar_one_or_none()
        if not supplier:
            raise NotFoundError(f"Supplier with id {supplier_id} not found")
        return supplier

    async def get_by_name(self, name: str) -> Supplier:
        """Get a supplier by name, case-insensitively."""
        result = await self.db.execute(
            select(Supplier)
            .where(func.lower(Supplier.name) == str(name).strip().lower())
            .execution_options(populate_existing=True)
        )
        supplier = result.scalar_one_or_none()
        if not supplier:
            raise NotFoundError(f"Supplier named '{name}' not found")
        return supplier

    async def list_suppliers(
        self,
        *,
        page: int = 1,
        size: int = 20,
        status: str | None = None,
        search: str | None = None,
        preferred_only: bool = False,
        with_orders: bool | None = None,
    ) -> tuple[list[Supplier], int]:
        """List suppliers with filtering, search and pagination.

        ``search`` matches name, contact or account number, which is how a
        counter clerk finds a supplier: by who they remember dealing with, or by
        the number on the account.
        """
        stmt = select(Supplier)
        count_stmt = select(func.count()).select_from(Supplier)

        filters = []
        if status:
            try:
                filters.append(Supplier.status == SupplierStatus(str(status).upper()).value)
            except ValueError:
                # An unknown status matches nothing rather than everything: a
                # mistyped filter must never silently return the whole list.
                filters.append(Supplier.id.is_(None))
        if search:
            term = f"%{str(search).strip().lower()}%"
            filters.append(
                or_(
                    func.lower(Supplier.name).like(term),
                    func.lower(func.coalesce(Supplier.contact_name, "")).like(term),
                    func.lower(func.coalesce(Supplier.account_number, "")).like(term),
                )
            )
        if preferred_only:
            filters.append(Supplier.is_preferred.is_(True))
        if with_orders is not None:
            # "Has the shop ever ordered from them?" is the question behind
            # retiring a supplier cleanly, so it is a filter rather than a
            # per-row count.
            has_orders = (
                select(PurchaseOrder.id)
                .where(PurchaseOrder.supplier_id == Supplier.id)
                .exists()
            )
            filters.append(has_orders if with_orders else ~has_orders)

        for condition in filters:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        total = (await self.db.execute(count_stmt)).scalar_one()

        offset = (page - 1) * size
        stmt = stmt.order_by(Supplier.name.asc()).offset(offset).limit(size)
        suppliers = list((await self.db.execute(stmt)).scalars().all())
        return suppliers, total

    async def get_summary(self, supplier_id: UUID | str) -> SupplierSummary:
        """What the shop has bought from one supplier.

        Spend is counted from orders that actually received goods, so a draft or
        cancelled order never inflates what the supplier is credited with. Units
        come from the receipts on the lines rather than from what was ordered,
        for the same reason.
        """
        supplier = await self.get_by_id(supplier_id)

        received_filter = PurchaseOrder.status.in_(RECEIVED_PURCHASE_ORDER_STATUSES)
        totals = (
            await self.db.execute(
                select(
                    func.count(PurchaseOrder.id),
                    func.count(PurchaseOrder.id).filter(
                        PurchaseOrder.status.in_(OPEN_PURCHASE_ORDER_STATUSES)
                    ),
                    func.count(PurchaseOrder.id).filter(received_filter),
                    func.coalesce(
                        func.sum(PurchaseOrder.total_amount).filter(received_filter), 0.0
                    ),
                    func.max(PurchaseOrder.order_date).filter(received_filter),
                    func.max(PurchaseOrder.received_at).filter(received_filter),
                ).where(PurchaseOrder.supplier_id == str(supplier.id))
            )
        ).one()
        total_orders, open_orders, received_orders, spend, last_order, last_received = totals

        units = (
            await self.db.execute(
                select(func.coalesce(func.sum(PurchaseOrderItem.quantity_received), 0.0))
                .select_from(PurchaseOrderItem)
                .join(PurchaseOrder, PurchaseOrderItem.purchase_order_id == PurchaseOrder.id)
                .where(
                    PurchaseOrder.supplier_id == str(supplier.id),
                    received_filter,
                )
            )
        ).scalar_one()

        return SupplierSummary(
            supplier_id=supplier.id,
            supplier_name=supplier.name,
            status=supplier.status,
            is_preferred=supplier.is_preferred,
            total_orders=int(total_orders or 0),
            open_orders=int(open_orders or 0),
            received_orders=int(received_orders or 0),
            total_units_received=round(float(units or 0.0), 2),
            total_spend=round(float(spend or 0.0), 2),
            last_order_date=last_order,
            last_received_at=last_received,
        )

    # --- writes -------------------------------------------------------------

    async def create_supplier(
        self, data: SupplierCreate, created_by_id: UUID | str | None = None
    ) -> Supplier:
        """Add a supplier to the list."""
        supplier = Supplier(
            name=data.name.strip(),
            contact_name=data.contact_name,
            email=data.email,
            phone=data.phone,
            address_line1=data.address_line1,
            address_line2=data.address_line2,
            city=data.city,
            state=data.state,
            postal_code=data.postal_code,
            country=data.country,
            account_number=data.account_number,
            website=data.website,
            lead_time_days=data.lead_time_days,
            payment_terms=data.payment_terms,
            notes=data.notes,
            is_preferred=data.is_preferred,
            status=SupplierStatus.ACTIVE.value,
            created_by_id=str(created_by_id) if created_by_id else None,
        )
        self.db.add(supplier)
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError(f"A supplier named '{data.name}' already exists")
        await self.db.refresh(supplier)
        logger.info("Supplier added: %s", supplier.name)
        return supplier

    async def update_supplier(
        self, supplier_id: UUID | str, update_data: SupplierUpdate
    ) -> Supplier:
        """Edit a supplier's details, trading status or preference."""
        supplier = await self.get_by_id(supplier_id)

        changes = update_data.model_dump(exclude_unset=True)
        if not changes:
            return supplier

        if changes.get("name"):
            changes["name"] = changes["name"].strip()
        for field, value in changes.items():
            setattr(supplier, field, value)

        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("Supplier update conflicts with an existing supplier")
        await self.db.refresh(supplier)
        return supplier

    async def deactivate(self, supplier_id: UUID | str) -> Supplier:
        """Stop buying from a supplier without losing the order history.

        A retired supplier also loses preferred status: a supplier the shop has
        stopped trading with must not stay on the "order here" shortlist.
        """
        supplier = await self.get_by_id(supplier_id)
        if supplier.status == SupplierStatus.INACTIVE.value:
            return supplier
        supplier.status = SupplierStatus.INACTIVE.value
        supplier.is_preferred = False
        await self.db.commit()
        await self.db.refresh(supplier)
        logger.info("Supplier deactivated: %s", supplier.name)
        return supplier

    async def delete_supplier(self, supplier_id: UUID | str) -> None:
        """Delete a supplier that has never been ordered from.

        A supplier the shop has placed orders with is a fact about what was
        bought and at what price, so it cannot be deleted — only marked
        ``INACTIVE``.
        """
        supplier = await self.get_by_id(supplier_id)

        orders = (
            await self.db.execute(
                select(func.count())
                .select_from(PurchaseOrder)
                .where(PurchaseOrder.supplier_id == str(supplier.id))
            )
        ).scalar_one()
        if orders:
            raise BusinessRuleError(
                f"Supplier {supplier.name} has {orders} purchase order(s); its history "
                "cannot be deleted. Mark it INACTIVE instead"
            )

        await self.db.delete(supplier)
        await self.db.commit()
        logger.info("Supplier deleted: %s", supplier_id)

    async def assert_orderable(self, supplier_id: UUID | str) -> Supplier:
        """Resolve a supplier for a new order, rejecting a retired one.

        A draft order against a retired supplier would be nonsense: nothing would
        ever be delivered against it. Reactivating the supplier first makes that a
        deliberate act.
        """
        supplier = await self.get_by_id(supplier_id)
        if supplier.status != SupplierStatus.ACTIVE.value:
            raise BusinessRuleError(
                f"Supplier {supplier.name} is {supplier.status} and cannot be ordered "
                "from; reactivate it first"
            )
        return supplier


__all__ = [
    "OPEN_PURCHASE_ORDER_STATUSES",
    "RECEIVED_PURCHASE_ORDER_STATUSES",
    "SupplierService",
]
