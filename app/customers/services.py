"""Customer management business logic.

Handles customer CRUD operations, address management, and customer
business rules.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.common.exceptions import (
    ConflictError,
    NotFoundError,
)
from app.customers.models import Address, Customer
from app.customers.schemas import CustomerCreate, CustomerUpdate

logger = logging.getLogger("autofix.customers.services")


class CustomerService:
    """Service for customer management operations."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, customer_id: UUID | str, *, include_inactive: bool = False) -> Customer:
        """Get a customer by ID."""
        stmt = select(Customer).where(Customer.id == str(customer_id))
        if not include_inactive:
            stmt = stmt.where(Customer.customer_status != "INACTIVE")
        result = await self.db.execute(stmt)
        customer = result.scalar_one_or_none()
        if not customer:
            raise NotFoundError(f"Customer with id {customer_id} not found")
        return customer

    async def get_by_user_id(self, user_id: UUID | str) -> Customer | None:
        """Get a customer linked to a specific user account."""
        stmt = select(Customer).where(Customer.user_id == str(user_id))
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_customers(
        self,
        *,
        page: int = 1,
        size: int = 20,
        search: str | None = None,
        customer_status: str | None = None,
    ) -> tuple[list[Customer], int]:
        """List customers with filtering and pagination."""
        stmt = select(Customer)
        count_stmt = select(func.count()).select_from(Customer)

        if customer_status:
            stmt = stmt.where(Customer.customer_status == customer_status)
            count_stmt = count_stmt.where(Customer.customer_status == customer_status)

        if search:
            like_pattern = f"%{search}%"
            stmt = stmt.where(
                (Customer.first_name.ilike(like_pattern))
                | (Customer.last_name.ilike(like_pattern))
                | (Customer.company_name.ilike(like_pattern))
                | (Customer.email.ilike(like_pattern))
                | (Customer.phone.ilike(like_pattern))
            )
            count_stmt = count_stmt.where(
                (Customer.first_name.ilike(like_pattern))
                | (Customer.last_name.ilike(like_pattern))
                | (Customer.company_name.ilike(like_pattern))
                | (Customer.email.ilike(like_pattern))
                | (Customer.phone.ilike(like_pattern))
            )

        count_result = await self.db.execute(count_stmt)
        total = count_result.scalar_one()

        offset = (page - 1) * size
        stmt = stmt.order_by(Customer.last_name, Customer.first_name).offset(offset).limit(size)
        result = await self.db.execute(stmt)
        customers = result.scalars().all()

        return customers, total

    async def search(self, query: str, limit: int = 20) -> list[Customer]:
        """Search customers by name, email, phone, or company."""
        like_pattern = f"%{query}%"
        stmt = (
            select(Customer)
            .where(
                (Customer.first_name.ilike(like_pattern))
                | (Customer.last_name.ilike(like_pattern))
                | (Customer.company_name.ilike(like_pattern))
                | (Customer.email.ilike(like_pattern))
                | (Customer.phone.ilike(like_pattern))
            )
            .order_by(Customer.last_name, Customer.first_name)
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def create_customer(self, customer_data: CustomerCreate) -> Customer:
        """Create a new customer."""
        # Check for duplicate email if provided
        if customer_data.email:
            existing = await self.db.execute(
                select(Customer).where(Customer.email == customer_data.email)
            )
            if existing.scalar_one_or_none():
                raise ConflictError(f"Customer with email {customer_data.email} already exists")

        customer = Customer(
            first_name=customer_data.first_name,
            last_name=customer_data.last_name,
            company_name=customer_data.company_name,
            email=customer_data.email,
            phone=customer_data.phone,
             preferred_contact=customer_data.preferred_contact,
             customer_status=customer_data.customer_status,
            notes=customer_data.notes,
        )

        # Handle addresses
        if customer_data.addresses:
            for addr_data in customer_data.addresses:
                address = Address(
                    street=addr_data.street,
                    city=addr_data.city,
                    state=addr_data.state,
                    postal_code=addr_data.postal_code,
                    country=addr_data.country,
                    address_type=addr_data.address_type,
                    customer=customer,
                )
                self.db.add(address)

        self.db.add(customer)
        await self.db.commit()
        await self.db.refresh(customer)
        logger.info("Customer created: %s %s", customer.first_name, customer.last_name)
        return customer

    async def update_customer(self, customer_id: UUID | str, customer_data: CustomerUpdate) -> Customer:
        """Update an existing customer."""
        customer = await self.get_by_id(customer_id)

        update_data = customer_data.model_dump(exclude_unset=True, exclude={"addresses"})

        for field, value in update_data.items():
            if value is not None:
                setattr(customer, field, value)

        # Handle addresses
        if customer_data.addresses is not None:
            # Clear existing addresses
            await self.db.execute(
                Address.__table__.delete().where(Address.customer_id == customer.id)
            )
            # Add new addresses
            for addr_data in customer_data.addresses:
                address = Address(
                    street=addr_data.street,
                    city=addr_data.city,
                    state=addr_data.state,
                    postal_code=addr_data.postal_code,
                    country=addr_data.country,
                    address_type=addr_data.address_type,
                    customer_id=customer.id,
                )
                self.db.add(address)

        await self.db.commit()
        await self.db.refresh(customer)
        logger.info("Customer updated: %s", customer.id)
        return customer

    async def delete_customer(self, customer_id: UUID | str) -> None:
        """Soft delete a customer by setting status to INACTIVE."""
        customer = await self.get_by_id(customer_id)
        customer.customer_status = "INACTIVE"
        await self.db.commit()
        logger.info("Customer deactivated: %s", customer.id)

    async def link_user(self, customer_id: UUID | str, user_id: UUID | str) -> Customer:
        """Link a customer to a User account for portal access."""
        customer = await self.get_by_id(customer_id)
        # Verify user exists
        result = await self.db.execute(select(User).where(User.id == str(user_id)))
        user = result.scalar_one_or_none()
        if not user:
            raise NotFoundError(f"User with id {user_id} not found")
        customer.user_id = str(user_id)
        await self.db.commit()
        await self.db.refresh(customer)
        logger.info("Customer %s linked to user %s", customer.id, user_id)
        return customer

    async def get_customer_vehicles(self, customer_id: UUID | str):
        """Get all vehicles for a customer."""
        from app.vehicles.models import Vehicle

        stmt = select(Vehicle).where(Vehicle.customer_id == str(customer_id))
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def get_service_history(self, customer_id: UUID | str):
        """Get service history for a customer's vehicles."""
        from app.repair_orders.models import RepairOrder
        from app.vehicles.models import Vehicle

        stmt = (
            select(RepairOrder)
            .join(Vehicle, Vehicle.customer_id == customer_id)
            .where(RepairOrder.customer_id == str(customer_id))
            .order_by(RepairOrder.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return result.scalars().all()
