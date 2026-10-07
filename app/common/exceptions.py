"""Custom HTTP exceptions for the application.

Provides specific error codes that clients can handle programmatically.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status


class AutoFixException(HTTPException):
    """Base exception for all custom application exceptions."""

    error_code: str = "INTERNAL_ERROR"

    def __init__(
        self,
        detail: str = "An error occurred",
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
        error_code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if error_code:
            self.error_code = error_code
        super().__init__(status_code=status_code, detail=detail, headers=headers)


class NotFoundError(AutoFixException):
    """Resource not found."""

    error_code = "NOT_FOUND"

    def __init__(self, detail: str = "Resource not found") -> None:
        super().__init__(detail=detail, status_code=status.HTTP_404_NOT_FOUND)


class ConflictError(AutoFixException):
    """Resource conflict (duplicate, state conflict)."""

    error_code = "CONFLICT"

    def __init__(self, detail: str = "Resource conflict") -> None:
        super().__init__(detail=detail, status_code=status.HTTP_409_CONFLICT)


class PermissionDeniedError(AutoFixException):
    """Insufficient permissions for the requested action."""

    error_code = "PERMISSION_DENIED"

    def __init__(self, detail: str = "You do not have permission to perform this action") -> None:
        super().__init__(detail=detail, status_code=status.HTTP_403_FORBIDDEN)


class AuthenticationError(AutoFixException):
    """Authentication required or token invalid."""

    error_code = "AUTHENTICATION_ERROR"

    def __init__(self, detail: str = "Authentication required") -> None:
        super().__init__(detail=detail, status_code=status.HTTP_401_UNAUTHORIZED)


class ValidationError(AutoFixException):
    """Business logic validation error."""

    error_code = "VALIDATION_ERROR"

    def __init__(
        self,
        detail: str = "Validation failed",
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(detail=detail, status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.errors = errors or []


class BusinessRuleError(AutoFixException):
    """Business rule violation."""

    error_code = "BUSINESS_RULE_VIOLATION"

    def __init__(self, detail: str = "Business rule violation") -> None:
        super().__init__(detail=detail, status_code=status.HTTP_400_BAD_REQUEST)
