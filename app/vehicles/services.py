"""Vehicle management business logic.

Handles vehicle CRUD operations, VIN validation, mileage tracking,
and vehicle-customer relationship logic.
"""

from __future__ import annotations

import logging
import re
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.vehicles.models import Vehicle, VehicleMileageRecord, VehicleStatus
from app.vehicles.schemas import VehicleCreate, VehicleUpdate

logger = logging.getLogger("autofix.vehicles.services")

VIN_PATTERN = re.compile(r"^[A-HJ-NPR-Z0-9]{11,17}$")


async def validate_customer_exists(db: AsyncSession, customer_id: UUID | str) -> bool:
    """Check that a customer exists (lazy import to avoid circular deps)."""
    from app.customers.models import Customer

    result = await db.execute(select(Customer).where(Customer.id == str(customer_id)))
    return result.scalar_one_or_none() is not None


def is_valid_vin(vin: str) -> bool:
    """Validate VIN format (11-17 alphanumeric chars, no I/O/Q)."""
    if not vin:
        return True  # VIN is optional
    return bool(VIN_PATTERN.match(vin.upper()))


def normalize_vin(vin: str | None) -> str | None:
    """Normalize VIN to uppercase and strip whitespace."""
    if vin is None:
        return None
    return vin.strip().upper() or None


class VehicleService:
    """Service for vehicle management operations."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, vehicle_id: UUID | str) -> Vehicle:
        """Get a vehicle by ID."""
        result = await self.db.execute(select(Vehicle).where(Vehicle.id == str(vehicle_id)))
        vehicle = result.scalar_one_or_none()
        if not vehicle:
            raise NotFoundError(f"Vehicle with id {vehicle_id} not found")
        return vehicle

    async def list_vehicles(
        self,
        *,
        page: int = 1,
        size: int = 20,
        customer_id: UUID | str | None = None,
        search: str | None = None,
    ) -> tuple[list[Vehicle], int]:
        """List vehicles with filtering and pagination."""
        stmt = select(Vehicle)
        count_stmt = select(func.count()).select_from(Vehicle)

        if customer_id:
            stmt = stmt.where(Vehicle.customer_id == str(customer_id))
            count_stmt = count_stmt.where(Vehicle.customer_id == str(customer_id))

        if search:
            like_pattern = f"%{search}%"
            stmt = stmt.where(
                (Vehicle.make.ilike(like_pattern))
                | (Vehicle.model.ilike(like_pattern))
                | (Vehicle.license_plate.ilike(like_pattern))
                | (Vehicle.vin.ilike(like_pattern))
            )
            count_stmt = count_stmt.where(
                (Vehicle.make.ilike(like_pattern))
                | (Vehicle.model.ilike(like_pattern))
                | (Vehicle.license_plate.ilike(like_pattern))
                | (Vehicle.vin.ilike(like_pattern))
            )

        count_result = await self.db.execute(count_stmt)
        total = count_result.scalar_one()

        offset = (page - 1) * size
        stmt = stmt.order_by(Vehicle.make, Vehicle.model).offset(offset).limit(size)
        result = await self.db.execute(stmt)
        vehicles = result.scalars().all()

        return vehicles, total

    async def search(self, query: str, limit: int = 20) -> list[Vehicle]:
        """Search vehicles by make, model, license plate, or VIN."""
        like_pattern = f"%{query}%"
        stmt = (
            select(Vehicle)
            .where(
                (Vehicle.make.ilike(like_pattern))
                | (Vehicle.model.ilike(like_pattern))
                | (Vehicle.license_plate.ilike(like_pattern))
                | (Vehicle.vin.ilike(like_pattern))
            )
            .order_by(Vehicle.make, Vehicle.model)
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def create_vehicle(self, vehicle_data: VehicleCreate) -> Vehicle:
        """Create a new vehicle."""
        if not await validate_customer_exists(self.db, vehicle_data.customer_id):
            raise ConflictError(f"Customer with id {vehicle_data.customer_id} not found")

        normalized_vin = normalize_vin(vehicle_data.vin)
        if normalized_vin and not is_valid_vin(normalized_vin):
            raise BusinessRuleError(f"Invalid VIN format: {vehicle_data.vin}")

        # The base schema keeps enum values as plain strings, so a
        # status the client sent arrives as a str while the default is
        # the member itself; both spellings end up as the column value.
        status_value = vehicle_data.status
        if isinstance(status_value, VehicleStatus):
            status_value = status_value.value
        vehicle = Vehicle(
            vin=normalized_vin,
            license_plate=vehicle_data.license_plate,
            make=vehicle_data.make,
            model=vehicle_data.model,
            year=vehicle_data.year,
            trim=vehicle_data.trim,
            engine=vehicle_data.engine,
            transmission=vehicle_data.transmission,
            mileage=vehicle_data.mileage,
            color=vehicle_data.color,
            fuel_type=vehicle_data.fuel_type,
            purchase_date=vehicle_data.purchase_date,
            notes=vehicle_data.notes,
            status=status_value or VehicleStatus.ACTIVE.value,
            customer_id=str(vehicle_data.customer_id),
        )

        self.db.add(vehicle)
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError(f"A vehicle with VIN {normalized_vin or '(none)'} already exists")
        await self.db.refresh(vehicle)
        logger.info("Vehicle created: %s %s (%s)", vehicle.make, vehicle.model, str(vehicle.id))
        return vehicle

    async def update_vehicle(self, vehicle_id: UUID | str, vehicle_data: VehicleUpdate) -> Vehicle:
        """Update an existing vehicle."""
        vehicle = await self.get_by_id(vehicle_id)

        normalized_vin = normalize_vin(vehicle_data.vin)
        if normalized_vin and not is_valid_vin(normalized_vin):
            from app.common.exceptions import BusinessRuleError
            raise BusinessRuleError(f"Invalid VIN format: {vehicle_data.vin}")

        update_data = vehicle_data.model_dump(exclude_unset=True)

        for field, value in update_data.items():
            if value is not None:
                setattr(vehicle, field, value)

        # Normalize VIN after update
        if vehicle.vin:
            vehicle.vin = normalize_vin(vehicle.vin)

        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("Vehicle update conflicts with an existing vehicle (duplicate VIN)")
        await self.db.refresh(vehicle)
        logger.info("Vehicle updated: %s", vehicle.id)
        return vehicle

    async def delete_vehicle(self, vehicle_id: UUID | str) -> None:
        """Delete a vehicle (hard delete - cascades to mileage records)."""
        vehicle = await self.get_by_id(vehicle_id)
        await self.db.delete(vehicle)
        await self.db.commit()
        logger.info("Vehicle deleted: %s", vehicle.id)

    async def add_mileage_record(self, vehicle_id: UUID | str, mileage: int, source: str = "MANUAL", notes: str | None = None) -> VehicleMileageRecord:
        """Add a mileage record and update the vehicle's current mileage."""
        vehicle = await self.get_by_id(vehicle_id)

        if mileage > 0:
            vehicle.mileage = mileage

        record = VehicleMileageRecord(
            vehicle_id=str(vehicle_id),
            mileage=mileage,
            source=source,
            notes=notes,
        )

        self.db.add(record)
        await self.db.commit()
        await self.db.refresh(record)
        logger.info("Mileage record added for vehicle %s: %d miles", vehicle_id, mileage)
        return record

    async def get_mileage_history(self, vehicle_id: UUID | str) -> list[VehicleMileageRecord]:
        """Get mileage history for a vehicle."""
        await self.get_by_id(vehicle_id)
        stmt = select(VehicleMileageRecord).where(VehicleMileageRecord.vehicle_id == str(vehicle_id))
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def set_status(self, vehicle_id: UUID | str, status: str) -> Vehicle:
        """Update vehicle status."""
        try:
            VehicleStatus(status)
        except ValueError:
            raise BusinessRuleError(f"Invalid vehicle status: {status}")

        vehicle = await self.get_by_id(vehicle_id)
        vehicle.status = status
        await self.db.commit()
        await self.db.refresh(vehicle)
        return vehicle
