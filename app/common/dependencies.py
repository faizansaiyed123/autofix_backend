"""Common FastAPI dependencies shared across modules.

Provides pagination, common query helpers, and utility dependencies.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.common.exceptions import NotFoundError
from app.core.database import get_session
from app.customers.models import Customer
from app.portal.services import PortalService


class PaginationParams:
    """Standard pagination parameters extracted from query string."""

    def __init__(
        self,
        page: int = Query(1, ge=1, description="Page number"),
        size: int = Query(20, ge=1, le=100, description="Items per page"),
        sort: str | None = Query(None, description="Sort field (prefix - for desc)"),
    ):
        self.page = page
        self.size = size
        self.sort = sort

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size

    @property
    def limit(self) -> int:
        return self.size


class Paginator:
    """Helper to paginate SQLAlchemy queries."""

    @staticmethod
    def apply(stmt, page: int, size: int):
        """Apply LIMIT/OFFSET to a query."""
        offset = (page - 1) * size
        return stmt.offset(offset).limit(size)


async def get_portal_customer(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Customer:
    """Resolve the customer record behind the signed-in user.

    The portal is scoped to *one* customer, resolved here rather than passed
    around: a route that accepted a customer id from the caller would be a route
    that could be pointed at somebody else's account. Deriving it from the
    authenticated user means there is no parameter to get wrong.
    """
    result = await session.execute(
        select(Customer).where(Customer.user_id == current_user.id)
    )
    customer = result.scalar_one_or_none()
    if not customer:
        # A signed-in user with no customer record has no portal. This is a 404
        # rather than a 403 because there is nothing here they are forbidden from
        # seeing — there is nothing here at all.
        raise NotFoundError("This account has no customer record attached to it")
    return customer


async def get_portal_service(
    customer: Annotated[Customer, Depends(get_portal_customer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PortalService:
    """A portal service already scoped to the signed-in customer.

    Every portal route depends on this rather than building its own, so the
    ownership rule is applied in exactly one place and no route can forget it.
    """
    return PortalService(session, customer)
