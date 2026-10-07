"""Shared utility functions.

Provides UUID generation, money formatting, pagination helpers, etc.
"""

from __future__ import annotations

import base64
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import asc, desc
from sqlalchemy.sql import Select

from app.common.ids import uuid7, uuid7_str


def generate_uuid() -> uuid.UUID:
    """Generate a UUID v7 (time-ordered) for use as primary key."""
    return uuid7()


def generate_uuid_str() -> str:
    """Generate a UUID v7 as a string."""
    return uuid7_str()


def is_valid_uuid(value: Any) -> bool:
    """Check if a value is a valid UUID string."""
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def parse_uuid(value: Any) -> uuid.UUID:
    """Parse a value as UUID, raising ValueError if invalid."""
    return uuid.UUID(str(value))


def money_to_float(value: Decimal | str | float | None) -> float:
    """Convert a monetary value to float (2 decimal places)."""
    if value is None:
        return 0.0
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


def format_currency(amount: Decimal | float | None, currency: str = "USD") -> str:
    """Format a monetary value as a currency string."""
    if amount is None:
        amount = 0.0
    return f"{currency} {float(amount):.2f}"


def paginate_query(
    query: Select,
    page: int = 1,
    size: int = 20,
    sort: str | None = None,
    model: type | None = None,
) -> Select:
    """Apply pagination and sorting to a SQLAlchemy query.

    Args:
        query: The base SQLAlchemy select query.
        page: Page number (1-indexed).
        size: Items per page (capped at 100).
        sort: Sort field, prefix with '-' for descending (e.g., '-created_at').
        model: The model class for resolving sort field names.

    Returns:
        The modified query with LIMIT/OFFSET and ORDER BY applied.
    """
    page = max(1, page)
    size = max(1, min(size, 100))
    offset = (page - 1) * size

    if sort and model:
        descending = sort.startswith("-")
        sort_field = sort[1:] if descending else sort
        if hasattr(model, sort_field):
            column = getattr(model, sort_field)
            query = query.order_by(desc(column) if descending else asc(column))

    return query.offset(offset).limit(size)


def encode_file_to_base64(file_path: str) -> str:
    """Read a file and encode its contents as base64."""
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def generate_file_hash(content: bytes) -> str:
    """Generate a SHA-256 hash for file content deduplication."""
    import hashlib

    return hashlib.sha256(content).hexdigest()


def generate_file_filename(file_hash: str, original_ext: str) -> str:
    """Generate a secure filename from hash and extension."""
    if not original_ext.startswith("."):
        original_ext = f".{original_ext}"
    return f"{file_hash}{original_ext.lower()}"


def get_file_extension(filename: str) -> str:
    """Extract the extension from a filename (including the dot)."""
    import os

    _, ext = os.path.splitext(filename)
    return ext.lower() if ext else ""
