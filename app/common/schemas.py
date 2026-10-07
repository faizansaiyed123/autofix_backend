"""Common schemas used across the application.

Provides pagination, error response, and shared data structures.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class BaseSchema(BaseModel):
    """Base schema with common configuration."""

    model_config = {
        "from_attributes": True,
        "use_enum_values": True,
        "str_strip_whitespace": True,
    }


class ErrorDetail(BaseSchema):
    """Detail of a validation error."""

    loc: list[str | int]
    msg: str
    type: str


class ErrorResponse(BaseSchema):
    """Standard error response envelope."""

    error: dict[str, Any]


class ValidationErrorResponse(ErrorResponse):
    """Validation error response with details."""

    error: dict[str, Any] = Field(
        ...,
        example={
            "code": "VALIDATION_ERROR",
            "message": "Validation failed",
            "details": [
                {"loc": ["body", "email"], "msg": "field required", "type": "value_error.missing"}
            ],
        },
    )


class PaginationMeta(BaseSchema):
    """Pagination metadata."""

    page: int
    size: int
    total: int
    pages: int


class PaginatedResponse(BaseSchema, Generic[T]):
    """Generic paginated response envelope."""

    data: list[T]
    meta: PaginationMeta

    @classmethod
    def create(
        cls,
        items: list[T],
        page: int,
        size: int,
        total: int,
    ) -> PaginatedResponse[T]:
        """Create a paginated response from items and metadata."""
        pages = (total + size - 1) // size if size > 0 else 0
        return cls(
            data=items,
            meta=PaginationMeta(
                page=page,
                size=size,
                total=total,
                pages=pages,
            ),
        )


class SuccessResponse(BaseSchema, Generic[T]):
    """Success response envelope with data."""

    data: T
    meta: dict[str, Any] | None = None


class SortDirection(str):
    """Sort direction enumeration."""


class PaginationParams:
    """Standard pagination parameters.

    Used as a FastAPI dependency to extract page and size from query params.
    """

    def __init__(self, page: int = 1, size: int = 20, sort: str | None = None):
        self.page = max(1, page)
        self.size = max(1, min(size, 100))
        self.sort = sort

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size

    @property
    def limit(self) -> int:
        return self.size
