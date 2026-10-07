"""Pydantic schemas for purchase orders and receiving."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import Field, field_validator, model_validator

from app.common.schemas import BaseSchema
from app.purchase_orders.models import PurchaseOrderStatus


class PurchaseOrderItemCreate(BaseSchema):
    """One part to order.

    ``unit_cost`` is what the supplier quoted, not the catalog cost: a purchase
    order is an agreement with one supplier at one price, and the catalog is a
    separate fact that the order must not overwrite.
    """

    part_id: uuid.UUID
    quantity_ordered: float = Field(..., gt=0, le=1_000_000)
    unit_cost: float = Field(0.0, ge=0, le=1_000_000)
    notes: str | None = Field(None, max_length=1000)


class PurchaseOrderItemUpdate(BaseSchema):
    """Partial update of an order line, on a draft order only."""

    quantity_ordered: float | None = Field(None, gt=0, le=1_000_000)
    unit_cost: float | None = Field(None, ge=0, le=1_000_000)
    notes: str | None = Field(None, max_length=1000)


class PurchaseOrderCreate(BaseSchema):
    """Payload for raising a purchase order.

    At least one line is required: an order with nothing on it is not an order,
    and accepting one would create a document that can be sent to a supplier
    while committing the shop to nothing.
    """

    supplier_id: uuid.UUID
    order_date: date | None = None
    expected_delivery_date: date | None = None
    tax_amount: float = Field(0.0, ge=0, le=1_000_000)
    shipping_amount: float = Field(0.0, ge=0, le=1_000_000)
    currency: str = Field("USD", min_length=3, max_length=3)
    notes: str | None = Field(None, max_length=2000)
    internal_notes: str | None = Field(None, max_length=2000)
    items: list[PurchaseOrderItemCreate] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _check_lines(self) -> PurchaseOrderCreate:
        seen: set[uuid.UUID] = set()
        for item in self.items:
            if item.part_id in seen:
                # The same part twice would make the receipt maths ambiguous:
                # which of the two lines is now full?
                raise ValueError(
                    f"Part {item.part_id} appears on more than one line; "
                    "combine the quantities into a single line"
                )
            seen.add(item.part_id)
        if (
            self.expected_delivery_date is not None
            and self.order_date is not None
            and self.expected_delivery_date < self.order_date
        ):
            raise ValueError(
                "expected_delivery_date cannot be before the order's own order_date"
            )
        return self

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str) -> str:
        return v.strip().upper()


class PurchaseOrderUpdate(BaseSchema):
    """Partial update of a draft purchase order.

    Supplying ``items`` replaces every line, so a caller edits the order in one
    request rather than juggling add/remove endpoints against each other.
    """

    expected_delivery_date: date | None = None
    tax_amount: float | None = Field(None, ge=0, le=1_000_000)
    shipping_amount: float | None = Field(None, ge=0, le=1_000_000)
    currency: str | None = Field(None, min_length=3, max_length=3)
    notes: str | None = Field(None, max_length=2000)
    internal_notes: str | None = Field(None, max_length=2000)
    items: list[PurchaseOrderItemCreate] | None = Field(None, min_length=1)

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str | None) -> str | None:
        return v.strip().upper() if v is not None else None

    @model_validator(mode="after")
    def _check_lines(self) -> PurchaseOrderUpdate:
        if self.items is not None:
            seen: set[uuid.UUID] = set()
            for item in self.items:
                if item.part_id in seen:
                    raise ValueError(
                        f"Part {item.part_id} appears on more than one line; "
                        "combine the quantities into a single line"
                    )
                seen.add(item.part_id)
        return self


class PurchaseOrderFromLowStock(BaseSchema):
    """Raise a draft order covering the shop's low-stock parts.

    ``shortage_multiplier`` decides how far above the reorder level to buy:
    1.0 orders exactly enough to get back to the reorder point, 2.0 (the default)
    doubles the shortage, which is what a shop with regular reorder traffic
    actually wants rather than reordering every day.
    """

    supplier_id: uuid.UUID
    expected_delivery_date: date | None = None
    tax_amount: float = Field(0.0, ge=0, le=1_000_000)
    shipping_amount: float = Field(0.0, ge=0, le=1_000_000)
    notes: str | None = Field(None, max_length=2000)
    shortage_multiplier: float = Field(2.0, ge=1.0, le=10.0)
    part_ids: list[uuid.UUID] | None = None
    include_discontinued: bool = False


class ReceiveLine(BaseSchema):
    """How much of one ordered line has arrived."""

    item_id: uuid.UUID
    quantity: float = Field(..., gt=0, le=1_000_000)
    unit_cost: float | None = Field(None, ge=0, le=1_000_000)


class ReceiveRequest(BaseSchema):
    """A delivery booking-in against a sent order.

    Lines are grouped so the same line cannot appear twice: a duplicated line
    would be ambiguous about which half counts toward the ordered quantity.
    """

    items: list[ReceiveLine] = Field(..., min_length=1)
    notes: str | None = Field(None, max_length=2000)

    @model_validator(mode="after")
    def _unique_lines(self) -> ReceiveRequest:
        seen: set[uuid.UUID] = set()
        for line in self.items:
            if line.item_id in seen:
                raise ValueError(
                    f"Line {line.item_id} is listed twice; combine the quantities "
                    "into a single line"
                )
            seen.add(line.item_id)
        return self


class PurchaseOrderItemRead(BaseSchema):
    """One line of a purchase order."""

    id: uuid.UUID
    part_id: uuid.UUID
    line_number: int
    part_number: str
    part_name: str
    quantity_ordered: float
    quantity_received: float
    unit_cost: float
    line_total: float
    received_total: float
    quantity_outstanding: float
    is_fully_received: bool
    notes: str | None = None
    created_at: datetime
    updated_at: datetime


class PurchaseOrderRead(BaseSchema):
    """A purchase order as returned by the API."""

    id: uuid.UUID
    po_number: str
    supplier_id: uuid.UUID
    supplier_name: str | None = None
    status: str
    order_date: date
    expected_delivery_date: date | None = None
    subtotal: float
    tax_amount: float
    shipping_amount: float
    total_amount: float
    currency: str
    notes: str | None = None
    internal_notes: str | None = None
    sent_at: datetime | None = None
    received_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None
    created_by_id: uuid.UUID | None = None
    item_count: int
    total_units_ordered: float
    total_units_received: float
    is_fully_received: bool
    is_terminal: bool
    is_editable: bool
    is_overdue: bool
    items: list[PurchaseOrderItemRead]
    created_at: datetime
    updated_at: datetime


class PurchaseOrderSummary(BaseSchema):
    """Headline numbers across purchase orders, for a dashboard tile."""

    total_orders: int
    draft_orders: int
    open_orders: int
    received_orders: int
    cancelled_orders: int
    overdue_orders: int
    total_committed: float
    total_received_value: float


class ReceiptReference(BaseSchema):
    """The inventory receipt one received line produced."""

    item_id: uuid.UUID
    part_id: uuid.UUID
    part_number: str
    quantity: float
    unit_cost: float
    transaction_id: uuid.UUID


class ReceiveResult(BaseSchema):
    """What a booking-in did: the order, and the receipts it filed."""

    purchase_order: PurchaseOrderRead
    received_units: float
    receipts: list[ReceiptReference]


class StatusChangeRequest(BaseSchema):
    """A purchase-order status transition requested through the generic endpoint."""

    status: str
    reason: str | None = Field(None, max_length=2000)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        try:
            return PurchaseOrderStatus(str(v).upper()).value
        except ValueError:
            allowed = ", ".join(s.value for s in PurchaseOrderStatus)
            raise ValueError(f"Invalid purchase order status '{v}'. Allowed: {allowed}")


class CancelRequest(BaseSchema):
    """A cancellation, with the reason recorded against the order.

    The reason is optional — an order can be called off with nothing to add — so
    this carries no required fields and the body may be omitted entirely.
    """

    reason: str | None = Field(None, max_length=2000)


__all__ = [
    "CancelRequest",
    "PurchaseOrderCreate",
    "PurchaseOrderFromLowStock",
    "PurchaseOrderItemCreate",
    "PurchaseOrderItemRead",
    "PurchaseOrderItemUpdate",
    "PurchaseOrderRead",
    "PurchaseOrderSummary",
    "PurchaseOrderUpdate",
    "ReceiptReference",
    "ReceiveLine",
    "ReceiveRequest",
    "ReceiveResult",
    "StatusChangeRequest",
]
