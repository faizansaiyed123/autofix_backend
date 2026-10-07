"""Inspection business logic.

Handles inspection CRUD, item and photo management, status transitions,
and generation of the customer-facing visual inspection report.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.common.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.inspections.models import (
    ITEM_STATUS_SEVERITY,
    ITEM_STATUS_SEVERITY_COLOR,
    Inspection,
    InspectionItem,
    InspectionItemStatus,
    InspectionPhoto,
    InspectionStatus,
)
from app.inspections.schemas import (
    InspectionCreate,
    InspectionItemCreate,
    InspectionItemUpdate,
    InspectionReport,
    InspectionUpdate,
    ReportCategory,
    ReportItem,
    ReportSummary,
)

logger = logging.getLogger("autofix.inspections.services")

VALID_STATUS_TRANSITIONS: dict[str, list[str]] = {
    InspectionStatus.DRAFT.value: [
        InspectionStatus.IN_PROGRESS.value,
        InspectionStatus.CANCELLED.value,
    ],
    InspectionStatus.IN_PROGRESS.value: [
        InspectionStatus.COMPLETED.value,
        InspectionStatus.CANCELLED.value,
    ],
    InspectionStatus.COMPLETED.value: [InspectionStatus.CANCELLED.value],
    InspectionStatus.CANCELLED.value: [],
}

# An inspection may only be completed once it actually has findings.
_TERMINAL_STATUSES = {InspectionStatus.COMPLETED.value, InspectionStatus.CANCELLED.value}


def _validate_status_transition(current: str, new: str) -> None:
    """Raise unless the inspection status transition is allowed."""
    allowed = VALID_STATUS_TRANSITIONS.get(current, [])
    if new not in allowed:
        raise BusinessRuleError(
            f"Cannot transition inspection status from '{current}' to '{new}'. "
            f"Allowed: {allowed or 'none (terminal state)'}"
        )


class InspectionService:
    """Service for inspection management operations."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # --- reads --------------------------------------------------------------

    async def get_by_id(self, inspection_id: UUID | str) -> Inspection:
        """Get an inspection by ID, with items and their photos loaded.

        ``populate_existing`` forces already-identity-mapped instances to be
        refreshed from the query. Sessions are configured with
        ``expire_on_commit=False``, so without it a re-read in the same session
        can hand back a stale ``items`` collection after a write.
        """
        result = await self.db.execute(
            select(Inspection)
            .options(selectinload(Inspection.items).selectinload(InspectionItem.photos))
            .where(Inspection.id == str(inspection_id))
            .execution_options(populate_existing=True)
        )
        inspection = result.scalar_one_or_none()
        if not inspection:
            raise NotFoundError(f"Inspection with id {inspection_id} not found")
        return inspection

    async def list_inspections(
        self,
        *,
        page: int = 1,
        size: int = 20,
        status: str | None = None,
        vehicle_id: UUID | str | None = None,
        customer_id: UUID | str | None = None,
        technician_id: UUID | str | None = None,
    ) -> tuple[list[Inspection], int]:
        """List inspections with filtering and pagination."""
        stmt = select(Inspection).options(
            selectinload(Inspection.items).selectinload(InspectionItem.photos)
        )
        count_stmt = select(func.count()).select_from(Inspection)

        filters = []
        if status:
            filters.append(Inspection.status == status)
        if vehicle_id:
            filters.append(Inspection.vehicle_id == str(vehicle_id))
        if customer_id:
            filters.append(Inspection.customer_id == str(customer_id))
        if technician_id:
            filters.append(Inspection.technician_id == str(technician_id))

        for condition in filters:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        total = (await self.db.execute(count_stmt)).scalar_one()

        offset = (page - 1) * size
        stmt = stmt.order_by(Inspection.created_at.desc()).offset(offset).limit(size)
        inspections = list((await self.db.execute(stmt)).scalars().all())

        return inspections, total

    # --- validation helpers -------------------------------------------------

    async def _assert_related_records_exist(
        self,
        *,
        customer_id: UUID | str,
        vehicle_id: UUID | str,
        checkin_id: UUID | str | None = None,
        technician_id: UUID | str | None = None,
    ) -> None:
        """Verify FK targets exist, and that the vehicle belongs to the customer."""
        from app.auth.models import User
        from app.checkins.models import CheckIn
        from app.customers.models import Customer
        from app.vehicles.models import Vehicle

        customer = (
            await self.db.execute(select(Customer).where(Customer.id == str(customer_id)))
        ).scalar_one_or_none()
        if not customer:
            raise ConflictError(f"Customer with id {customer_id} not found")

        vehicle = (
            await self.db.execute(select(Vehicle).where(Vehicle.id == str(vehicle_id)))
        ).scalar_one_or_none()
        if not vehicle:
            raise ConflictError(f"Vehicle with id {vehicle_id} not found")

        if str(vehicle.customer_id) != str(customer_id):
            raise BusinessRuleError(
                f"Vehicle {vehicle_id} does not belong to customer {customer_id}"
            )

        if checkin_id:
            checkin = (
                await self.db.execute(select(CheckIn).where(CheckIn.id == str(checkin_id)))
            ).scalar_one_or_none()
            if not checkin:
                raise ConflictError(f"Check-in with id {checkin_id} not found")

        if technician_id:
            technician = (
                await self.db.execute(select(User).where(User.id == str(technician_id)))
            ).scalar_one_or_none()
            if not technician:
                raise ConflictError(f"Technician with id {technician_id} not found")

    async def _assert_editable(self, inspection: Inspection) -> None:
        """Reject changes to inspections that are completed or cancelled."""
        if inspection.status in _TERMINAL_STATUSES:
            raise BusinessRuleError(
                f"Inspection is {inspection.status} and can no longer be modified"
            )

    def _build_item(
        self, inspection_id: str, item_data: InspectionItemCreate
    ) -> tuple[InspectionItem, list[InspectionPhoto]]:
        """Construct an item and its photos (not yet added to the session)."""
        item = InspectionItem(
            inspection_id=inspection_id,
            category=item_data.category,
            item_name=item_data.item_name,
            status=item_data.status,
            measurement=item_data.measurement,
            notes=item_data.notes,
            recommendation=item_data.recommendation,
        )
        photos = [
            InspectionPhoto(
                inspection_id=inspection_id,
                photo_url=photo.photo_url,
                caption=photo.caption,
            )
            for photo in item_data.all_photos()
        ]
        return item, photos

    # --- writes -------------------------------------------------------------

    async def create_inspection(self, inspection_data: InspectionCreate) -> Inspection:
        """Create an inspection together with its initial items and photos.

        The whole creation runs as one transaction: either the inspection and
        all of its items land, or none of it does.
        """
        await self._assert_related_records_exist(
            customer_id=inspection_data.customer_id,
            vehicle_id=inspection_data.vehicle_id,
            checkin_id=inspection_data.checkin_id,
            technician_id=inspection_data.technician_id,
        )

        inspection = Inspection(
            vehicle_id=str(inspection_data.vehicle_id),
            customer_id=str(inspection_data.customer_id),
            checkin_id=str(inspection_data.checkin_id) if inspection_data.checkin_id else None,
            technician_id=(
                str(inspection_data.technician_id) if inspection_data.technician_id else None
            ),
            mileage=inspection_data.mileage,
            overall_notes=inspection_data.overall_notes,
        )
        self.db.add(inspection)
        await self.db.flush()

        for item_data in inspection_data.items:
            item, photos = self._build_item(str(inspection.id), item_data)
            self.db.add(item)
            await self.db.flush()
            for photo in photos:
                photo.inspection_item_id = str(item.id)
                self.db.add(photo)

        await self.db.commit()
        logger.info("Inspection created: %s", inspection.id)
        return await self.get_by_id(inspection.id)

    async def add_item(
        self, inspection_id: UUID | str, item_data: InspectionItemCreate
    ) -> InspectionItem:
        """Add an item (with optional photos) to an existing inspection."""
        inspection = await self.get_by_id(inspection_id)
        await self._assert_editable(inspection)

        item, photos = self._build_item(str(inspection.id), item_data)
        self.db.add(item)
        await self.db.flush()
        for photo in photos:
            photo.inspection_item_id = str(item.id)
            self.db.add(photo)

        await self.db.commit()
        await self.db.refresh(item)
        logger.info("Inspection item added: %s", item.id)
        return item

    async def update_item(
        self,
        inspection_id: UUID | str,
        item_id: UUID | str,
        update_data: InspectionItemUpdate,
    ) -> InspectionItem:
        """Update a single inspection item."""
        inspection = await self.get_by_id(inspection_id)
        await self._assert_editable(inspection)

        item = next((i for i in inspection.items if str(i.id) == str(item_id)), None)
        if not item:
            raise NotFoundError(
                f"Inspection item {item_id} not found on inspection {inspection_id}"
            )

        for field, value in update_data.model_dump(exclude_unset=True).items():
            if value is not None:
                setattr(item, field, value)

        await self.db.commit()
        await self.db.refresh(item)
        return item

    async def delete_item(self, inspection_id: UUID | str, item_id: UUID | str) -> None:
        """Remove an inspection item and its photos."""
        inspection = await self.get_by_id(inspection_id)
        await self._assert_editable(inspection)

        item = next((i for i in inspection.items if str(i.id) == str(item_id)), None)
        if not item:
            raise NotFoundError(
                f"Inspection item {item_id} not found on inspection {inspection_id}"
            )

        # Removing from the parent collection keeps the in-session state
        # consistent; the delete-orphan cascade issues the DELETE.
        inspection.items.remove(item)
        await self.db.commit()

    async def update_inspection(
        self, inspection_id: UUID | str, update_data: InspectionUpdate
    ) -> Inspection:
        """Update an inspection's status, technician, mileage or notes."""
        inspection = await self.get_by_id(inspection_id)
        data = update_data.model_dump(exclude_unset=True)

        new_status = data.pop("status", None)
        if new_status is not None:
            _validate_status_transition(inspection.status, new_status)
            self._assert_completable(inspection, new_status)

        if data and inspection.status in _TERMINAL_STATUSES and new_status is None:
            await self._assert_editable(inspection)

        for field, value in data.items():
            if value is not None:
                setattr(inspection, field, value)

        if new_status is not None:
            inspection.status = new_status

        await self.db.commit()
        return await self.get_by_id(inspection_id)

    def _assert_completable(self, inspection: Inspection, new_status: str) -> None:
        """An inspection with no recorded findings cannot be completed."""
        if new_status == InspectionStatus.COMPLETED.value and not inspection.items:
            raise BusinessRuleError(
                "Cannot complete an inspection that has no inspection items recorded"
            )

    async def update_status(self, inspection_id: UUID | str, status: str) -> Inspection:
        """Update inspection status with transition validation."""
        try:
            status = InspectionStatus(status.upper()).value
        except (ValueError, AttributeError):
            allowed = ", ".join(s.value for s in InspectionStatus)
            raise BusinessRuleError(f"Invalid inspection status: {status}. Allowed: {allowed}")

        inspection = await self.get_by_id(inspection_id)
        _validate_status_transition(inspection.status, status)
        self._assert_completable(inspection, status)

        inspection.status = status
        await self.db.commit()
        return await self.get_by_id(inspection_id)

    async def delete_inspection(self, inspection_id: UUID | str) -> None:
        """Delete an inspection and everything hanging off it."""
        inspection = await self.get_by_id(inspection_id)
        await self.db.delete(inspection)
        await self.db.commit()
        logger.info("Inspection deleted: %s", inspection_id)

    # --- customer-facing report --------------------------------------------

    async def generate_report(self, inspection_id: UUID | str) -> InspectionReport:
        """Build the customer-friendly traffic-light inspection report.

        Items are grouped by category, each category takes the colour of its
        worst item, and urgent/recommended work is surfaced separately so the
        customer sees what needs attention without reading every line.
        """
        inspection = await self.get_by_id(inspection_id)

        def to_report_item(item: InspectionItem) -> ReportItem:
            return ReportItem.model_validate(item)

        categories: dict[str, list[InspectionItem]] = {}
        for item in inspection.items:
            categories.setdefault(item.category, []).append(item)

        report_categories: list[ReportCategory] = []
        for category_name, items in categories.items():
            worst = max(items, key=lambda i: ITEM_STATUS_SEVERITY.get(i.status, 0))
            report_categories.append(
                ReportCategory(
                    category=category_name,
                    severity_color=ITEM_STATUS_SEVERITY_COLOR.get(worst.status, "GREY"),
                    items=[to_report_item(i) for i in items],
                )
            )

        # Worst categories first, so the customer sees problems at the top.
        severity_rank = {"RED": 0, "YELLOW": 1, "GREEN": 2, "GREY": 3}
        report_categories.sort(key=lambda c: (severity_rank.get(c.severity_color, 3), c.category))

        colors = [ITEM_STATUS_SEVERITY_COLOR.get(i.status, "GREY") for i in inspection.items]
        summary = ReportSummary(
            total_items=len(inspection.items),
            green=colors.count("GREEN"),
            yellow=colors.count("YELLOW"),
            red=colors.count("RED"),
            not_checked=colors.count("GREY"),
        )

        urgent = [
            to_report_item(i)
            for i in inspection.items
            if i.status == InspectionItemStatus.URGENT.value
        ]
        recommended = [
            to_report_item(i)
            for i in inspection.items
            if i.status
            in (
                InspectionItemStatus.RECOMMENDED.value,
                InspectionItemStatus.ATTENTION.value,
            )
        ]

        return InspectionReport(
            inspection_id=inspection.id,
            status=inspection.status,
            vehicle_id=inspection.vehicle_id,
            customer_id=inspection.customer_id,
            mileage=inspection.mileage,
            overall_condition=inspection.overall_condition,
            summary=summary,
            categories=report_categories,
            urgent_items=urgent,
            recommended_items=recommended,
            overall_notes=inspection.overall_notes,
            inspected_at=inspection.created_at,
        )
