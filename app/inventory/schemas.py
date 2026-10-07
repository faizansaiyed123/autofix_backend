"""Pydantic schemas for the inventory ledger."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, model_validator

from app.common.schemas import BaseSchema
from app.inventory.models import (
    InventoryTransactionType,
    StockMovementDirection,
    direction_for,
)


class InventoryTransactionCreate(BaseSchema):
    """A movement of stock to record.

    ``quantity`` is how many units moved. The direction normally comes from the
    type — a RECEIPT arrives, an ISSUE leaves — and only ``ADJUSTMENT`` and
    ``TRANSFER``, which could go either way, need it stated.
    """

    part_id: uuid.UUID
    transaction_type: str
    quantity: float = Field(..., gt=0, le=1_000_000)
    direction: str | None = None
    unit_cost: float | None = Field(None, ge=0, le=1_000_000)
    repair_order_id: uuid.UUID | None = None
    reference: str | None = Field(None, max_length=100)
    reason: str | None = Field(None, max_length=1000)
    from_location: str | None = Field(None, max_length=50)
    to_location: str | None = Field(None, max_length=50)

    @model_validator(mode="after")
    def _check_movement(self) -> InventoryTransactionCreate:
        if self.direction is not None:
            try:
                StockMovementDirection(str(self.direction).upper())
            except ValueError:
                allowed = ", ".join(d.value for d in StockMovementDirection)
                raise ValueError(f"Invalid direction '{self.direction}'. Allowed: {allowed}")

        try:
            tx_type = InventoryTransactionType(str(self.transaction_type).upper())
        except ValueError:
            allowed = ", ".join(t.value for t in InventoryTransactionType)
            raise ValueError(
                f"Invalid inventory transaction type '{self.transaction_type}'. "
                f"Allowed: {allowed}"
            )

        try:
            direction_for(
                tx_type.value,
                str(self.direction).upper() if self.direction is not None else None,
            )
        except ValueError as exc:
            raise ValueError(str(exc))

        # A transfer that does not say where the stock went is a transfer whose
        # other half can never be found.
        if tx_type is InventoryTransactionType.TRANSFER and not (
            self.to_location or self.from_location
        ):
            raise ValueError(
                "a TRANSFER must name the other location (to_location when "
                "stock leaves, from_location when it arrives)"
            )
        return self


class InventoryTransactionRead(BaseSchema):
    """A recorded stock movement."""

    id: uuid.UUID
    part_id: uuid.UUID
    transaction_type: str
    quantity: float
    quantity_before: float
    quantity_after: float
    unit_cost: float | None = None
    repair_order_id: uuid.UUID | None = None
    reference: str | None = None
    performed_by_id: uuid.UUID | None = None
    reason: str | None = None
    from_location: str | None = None
    to_location: str | None = None
    created_at: datetime
    updated_at: datetime


class StockLevel(BaseSchema):
    """A part's balance with the valuation it represents."""

    part_id: uuid.UUID
    part_number: str
    name: str
    category: str
    location: str | None = None
    quantity_on_hand: float
    reorder_level: float
    unit_cost: float
    stock_value: float
    stock_status: str
    is_low_stock: bool


class LowStockAlert(BaseSchema):
    """A part at or below its reorder level, with how far short it is."""

    part_id: uuid.UUID
    part_number: str
    name: str
    category: str
    brand: str | None = None
    location: str | None = None
    quantity_on_hand: float
    reorder_level: float
    shortage: float
    stock_status: str


class StockSummary(BaseSchema):
    """Headline numbers across the whole shelf."""

    total_parts: int
    total_units: float
    total_stock_value: float
    low_stock_count: int
    out_of_stock_count: int


__all__ = [
    "InventoryTransactionCreate",
    "InventoryTransactionRead",
    "LowStockAlert",
    "StockLevel",
    "StockSummary",
]
