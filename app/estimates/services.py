"""Estimate business logic.

Owns estimate and line-item CRUD, the money calculation, the advisor status
machine, and the customer's per-item approval workflow (including partial
approvals and expiry).
"""

from __future__ import annotations

import logging
import uuid as uuid_module
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.common.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.estimates.models import (
    ESTIMATE_STATUS_TRANSITIONS,
    Estimate,
    EstimateItem,
    EstimateItemStatus,
    EstimateItemType,
    EstimateStatus,
    is_transition_allowed,
    round_money,
)
from app.estimates.schemas import (
    EstimateCreate,
    EstimateItemCounts,
    EstimateItemCreate,
    EstimateItemUpdate,
    EstimateSummary,
    EstimateTotals,
    EstimateUpdate,
    ItemDecision,
)
from app.notifications.events import EventPublisher
from app.notifications.services import (
    NotificationService,
    estimate_ready_event,
)

logger = logging.getLogger("autofix.estimates.services")

# Statuses whose line items may still be edited by staff.
EDITABLE_STATUSES = frozenset({EstimateStatus.DRAFT.value})

# Statuses that block deletion: the customer has seen or acted on them.
UNDELETABLE_STATUSES = frozenset(
    {
        EstimateStatus.SENT.value,
        EstimateStatus.PARTIALLY_APPROVED.value,
        EstimateStatus.APPROVED.value,
    }
)

# Statuses that mark the customer decision phase as finished.
_DECIDED_STATUSES = frozenset(
    {
        EstimateStatus.PARTIALLY_APPROVED.value,
        EstimateStatus.APPROVED.value,
        EstimateStatus.DECLINED.value,
    }
)

# A customer may keep deciding lines while the estimate is sent, and continues
# to do so after the first decision moves it to PARTIALLY_APPROVED.
DECIDABLE_STATUSES = frozenset(
    {
        EstimateStatus.SENT.value,
        EstimateStatus.PARTIALLY_APPROVED.value,
    }
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class EstimateService:
    """Service for estimate management and customer approval."""

    def __init__(self, db: AsyncSession):
        self.db = db
        # Notification events raised by this service, flushed with the change
        # that caused them. See app.notifications.events for why they are queued
        # rather than written inline.
        self.events = EventPublisher()

    # --- reads --------------------------------------------------------------

    async def get_by_id(self, estimate_id: UUID | str) -> Estimate:
        """Get an estimate by ID with its line items loaded.

        ``populate_existing`` refreshes identity-mapped instances; sessions run
        with ``expire_on_commit=False``, so a re-read in the same session would
        otherwise hand back a stale item collection after a write.
        """
        result = await self.db.execute(
            select(Estimate)
            .options(selectinload(Estimate.items))
            .where(Estimate.id == str(estimate_id))
            .execution_options(populate_existing=True)
        )
        estimate = result.scalar_one_or_none()
        if not estimate:
            raise NotFoundError(f"Estimate with id {estimate_id} not found")
        return estimate

    async def get_by_number(self, estimate_number: str) -> Estimate:
        """Look an estimate up by its human-facing number."""
        result = await self.db.execute(
            select(Estimate)
            .options(selectinload(Estimate.items))
            .where(Estimate.estimate_number == estimate_number)
            .execution_options(populate_existing=True)
        )
        estimate = result.scalar_one_or_none()
        if not estimate:
            raise NotFoundError(f"Estimate {estimate_number} not found")
        return estimate

    async def list_estimates(
        self,
        *,
        page: int = 1,
        size: int = 20,
        status: str | None = None,
        customer_id: UUID | str | None = None,
        vehicle_id: UUID | str | None = None,
        inspection_id: UUID | str | None = None,
    ) -> tuple[list[Estimate], int]:
        """List estimates with filtering and pagination."""
        stmt = select(Estimate).options(selectinload(Estimate.items))
        count_stmt = select(func.count()).select_from(Estimate)

        filters = []
        if status:
            filters.append(Estimate.status == status.upper())
        if customer_id:
            filters.append(Estimate.customer_id == str(customer_id))
        if vehicle_id:
            filters.append(Estimate.vehicle_id == str(vehicle_id))
        if inspection_id:
            filters.append(Estimate.inspection_id == str(inspection_id))

        for condition in filters:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        total = (await self.db.execute(count_stmt)).scalar_one()

        offset = (page - 1) * size
        stmt = stmt.order_by(Estimate.created_at.desc()).offset(offset).limit(size)
        estimates = list((await self.db.execute(stmt)).scalars().all())
        return estimates, total

    # --- validation helpers -------------------------------------------------

    async def _assert_related_records_exist(
        self,
        *,
        customer_id: UUID | str,
        vehicle_id: UUID | str,
        inspection_id: UUID | str | None = None,
        service_request_id: UUID | str | None = None,
        created_by_id: UUID | str | None = None,
    ) -> None:
        """Verify FK targets exist and belong to the same customer/vehicle."""
        from app.auth.models import User
        from app.customers.models import Customer
        from app.inspections.models import Inspection
        from app.service_requests.models import ServiceRequest
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

        if inspection_id:
            inspection = (
                await self.db.execute(
                    select(Inspection).where(Inspection.id == str(inspection_id))
                )
            ).scalar_one_or_none()
            if not inspection:
                raise ConflictError(f"Inspection with id {inspection_id} not found")
            if str(inspection.vehicle_id) != str(vehicle_id):
                raise BusinessRuleError(
                    f"Inspection {inspection_id} is for a different vehicle"
                )

        if service_request_id:
            request = (
                await self.db.execute(
                    select(ServiceRequest).where(ServiceRequest.id == str(service_request_id))
                )
            ).scalar_one_or_none()
            if not request:
                raise ConflictError(f"Service request with id {service_request_id} not found")
            if request.vehicle_id and str(request.vehicle_id) != str(vehicle_id):
                raise BusinessRuleError(
                    f"Service request {service_request_id} is for a different vehicle"
                )
            if str(request.customer_id) != str(customer_id):
                raise BusinessRuleError(
                    f"Service request {service_request_id} belongs to a different customer"
                )

        if created_by_id:
            user = (
                await self.db.execute(select(User).where(User.id == str(created_by_id)))
            ).scalar_one_or_none()
            if not user:
                raise ConflictError(f"User with id {created_by_id} not found")

    @staticmethod
    def _assert_editable(estimate: Estimate) -> None:
        """Reject line edits on an estimate the customer has already seen."""
        if estimate.status not in EDITABLE_STATUSES:
            raise BusinessRuleError(
                f"Estimate is {estimate.status}; only a DRAFT estimate can be edited"
            )

    @staticmethod
    def _next_sequence(estimate: Estimate) -> int:
        """Next display position for a line appended to the estimate."""
        return max((int(i.sequence or 0) for i in estimate.items), default=-1) + 1

    @staticmethod
    def _new_estimate_number() -> str:
        """Generate a human-readable, collision-resistant estimate number.

        A date prefix makes the number recognisable at the counter; the random
        suffix keeps concurrent creates from colliding, and the column is
        unique so any residual clash surfaces as a 409 rather than silent
        duplication.
        """
        stamp = datetime.now(UTC).strftime("%Y%m")
        suffix = uuid_module.uuid4().hex[:6].upper()
        return f"EST-{stamp}-{suffix}"

    def _build_item(
        self,
        item_data: EstimateItemCreate,
        sequence: int,
        estimate_id: str | None = None,
    ) -> EstimateItem:
        """Build a priced line for an estimate (not yet added to the session).

        ``estimate_id`` is optional: when the line is handed to an estimate's
        ``items`` collection, the relationship fills the foreign key in. Setting
        it explicitly is only needed for a line built outside that collection.
        """
        return EstimateItem(
            estimate_id=estimate_id,
            item_type=item_data.item_type,
            description=item_data.description,
            labor_hours=item_data.labor_hours,
            labor_rate=item_data.labor_rate,
            part_number=item_data.part_number,
            part_name=item_data.part_name,
            quantity=item_data.quantity,
            unit_price=item_data.unit_price,
            discount_amount=item_data.discount_amount,
            is_optional=item_data.is_optional,
            notes=item_data.notes,
            sequence=sequence,
        )

    # --- money --------------------------------------------------------------

    @staticmethod
    def recalculate(estimate: Estimate) -> None:
        """Recompute every line total and the estimate's money totals.

        Called after any change to lines, tax rate, or customer decisions so
        the stored totals can never drift from the lines that produced them.
        """
        charges = 0.0
        discounts = 0.0
        for item in estimate.items:
            item.line_total = item.compute_line_total()
            if item.is_discount:
                discounts += abs(item.line_total)
            else:
                charges += item.line_total

        subtotal = round_money(charges)
        discount_amount = round_money(discounts)
        # Discounts can never exceed the work they discount; the estimate
        # floors at zero rather than billing the customer a negative total.
        discount_amount = min(discount_amount, subtotal)
        taxable = round_money(max(subtotal - discount_amount, 0.0))
        tax_amount = round_money(taxable * float(estimate.tax_rate))

        estimate.subtotal = subtotal
        estimate.discount_amount = discount_amount
        estimate.tax_amount = tax_amount
        estimate.total = round_money(taxable + tax_amount)

    async def _recalculate_and_save(self, estimate: Estimate) -> Estimate:
        self.recalculate(estimate)
        await self.db.commit()
        # Notifications go in after the estimate is safely committed, so a
        # customer is never told about an estimate that failed to save. A failure
        # to notify must not undo work that succeeded, so it is logged and
        # swallowed rather than raised.
        await self._flush_events()
        return await self.get_by_id(estimate.id)

    async def _flush_events(self) -> None:
        """Write any queued notifications, never failing the caller's operation."""
        if not len(self.events):
            return
        try:
            await NotificationService(self.db).flush_events(self.events)
        except Exception:
            await self.db.rollback()
            logger.exception(
                "Failed to deliver notifications for estimate changes; "
                "the estimate itself is unaffected"
            )

    async def get_summary(self, estimate_id: UUID | str) -> EstimateSummary:
        """Build the customer-facing money summary for an estimate."""
        estimate = await self.get_by_id(estimate_id)
        counts = EstimateItemCounts(
            total=len(estimate.items),
            pending=sum(
                1 for i in estimate.items if i.status == EstimateItemStatus.PENDING.value
            ),
            approved=sum(
                1 for i in estimate.items if i.status == EstimateItemStatus.APPROVED.value
            ),
            declined=sum(
                1 for i in estimate.items if i.status == EstimateItemStatus.DECLINED.value
            ),
        )
        return EstimateSummary(
            estimate_id=estimate.id,
            estimate_number=estimate.estimate_number,
            status=estimate.status,
            is_expired=estimate.is_expired,
            can_decide=estimate.is_open_for_decision,
            valid_until=estimate.valid_until,
            totals=EstimateTotals(
                subtotal=float(estimate.subtotal),
                discount_amount=float(estimate.discount_amount),
                tax_rate=float(estimate.tax_rate),
                tax_amount=float(estimate.tax_amount),
                total=float(estimate.total),
                approved_total=estimate.approved_total,
            ),
            counts=counts,
        )

    # --- writes -------------------------------------------------------------

    async def create_estimate(
        self, estimate_data: EstimateCreate, created_by_id: UUID | str | None = None
    ) -> Estimate:
        """Create an estimate together with its initial lines.

        The header, every line, and the money totals land in one transaction:
        either the whole proposal exists or none of it does.
        """
        await self._assert_related_records_exist(
            customer_id=estimate_data.customer_id,
            vehicle_id=estimate_data.vehicle_id,
            inspection_id=estimate_data.inspection_id,
            service_request_id=estimate_data.service_request_id,
            created_by_id=created_by_id,
        )

        estimate = Estimate(
            estimate_number=self._new_estimate_number(),
            customer_id=str(estimate_data.customer_id),
            vehicle_id=str(estimate_data.vehicle_id),
            inspection_id=(
                str(estimate_data.inspection_id) if estimate_data.inspection_id else None
            ),
            service_request_id=(
                str(estimate_data.service_request_id)
                if estimate_data.service_request_id
                else None
            ),
            created_by_id=str(created_by_id) if created_by_id else None,
            valid_until=estimate_data.valid_until,
            tax_rate=estimate_data.tax_rate,
            notes=estimate_data.notes,
            # Seeding the collection through the constructor loads it up front.
            # Appending afterwards would touch the still-unloaded relationship
            # and trigger lazy IO outside the async greenlet context.
            items=[
                self._build_item(item_data, index)
                for index, item_data in enumerate(estimate_data.items)
            ],
        )
        self.db.add(estimate)
        await self.db.flush()

        self.recalculate(estimate)
        await self.db.commit()
        logger.info("Estimate created: %s", estimate.estimate_number)
        return await self.get_by_id(estimate.id)

    async def add_item(
        self, estimate_id: UUID | str, item_data: EstimateItemCreate
    ) -> Estimate:
        """Append a priced line to a draft estimate."""
        estimate = await self.get_by_id(estimate_id)
        self._assert_editable(estimate)

        item = self._build_item(
            item_data, self._next_sequence(estimate), estimate_id=str(estimate.id)
        )
        estimate.items.append(item)

        return await self._recalculate_and_save(estimate)

    async def update_item(
        self,
        estimate_id: UUID | str,
        item_id: UUID | str,
        update_data: EstimateItemUpdate,
    ) -> Estimate:
        """Update a line on a draft estimate and recompute the totals."""
        estimate = await self.get_by_id(estimate_id)
        self._assert_editable(estimate)

        item = next((i for i in estimate.items if str(i.id) == str(item_id)), None)
        if not item:
            raise NotFoundError(f"Estimate item {item_id} not found on estimate {estimate_id}")

        changes = {
            field: value
            for field, value in update_data.model_dump(exclude_unset=True).items()
            if value is not None
        }

        # Validate before mutating. Raising after a partial write would leave
        # the session dirty, and the next flush would then try to update a row
        # the caller never meant to change.
        self._assert_line_is_priceable(
            item_type=item.item_type,
            description=changes.get("description", item.description),
            labor_hours=changes.get("labor_hours", item.labor_hours),
            labor_rate=changes.get("labor_rate", item.labor_rate),
            unit_price=changes.get("unit_price", item.unit_price),
        )

        for field, value in changes.items():
            setattr(item, field, value)

        return await self._recalculate_and_save(estimate)

    @staticmethod
    def _assert_line_is_priceable(
        *,
        item_type: str,
        description: str,
        labor_hours: float | None,
        labor_rate: float | None,
        unit_price: float | None,
    ) -> None:
        """Reject a line that no longer carries the inputs its type needs."""
        if item_type == EstimateItemType.LABOR.value:
            if labor_hours is None or labor_rate is None:
                raise BusinessRuleError(
                    f"LABOR line '{description}' needs both labor_hours and labor_rate"
                )
        elif item_type in (
            EstimateItemType.PART.value,
            EstimateItemType.SERVICE.value,
            EstimateItemType.FEE.value,
            EstimateItemType.DISCOUNT.value,
        ) and not unit_price:
            raise BusinessRuleError(
                f"{item_type} line '{description}' needs a unit_price above zero"
            )

    async def delete_item(self, estimate_id: UUID | str, item_id: UUID | str) -> Estimate:
        """Remove a line from a draft estimate and recompute the totals."""
        estimate = await self.get_by_id(estimate_id)
        self._assert_editable(estimate)

        item = next((i for i in estimate.items if str(i.id) == str(item_id)), None)
        if not item:
            raise NotFoundError(f"Estimate item {item_id} not found on estimate {estimate_id}")

        # Removing from the collection keeps in-session state consistent; the
        # delete-orphan cascade issues the DELETE.
        estimate.items.remove(item)
        return await self._recalculate_and_save(estimate)

    async def update_estimate(
        self, estimate_id: UUID | str, update_data: EstimateUpdate
    ) -> Estimate:
        """Update a draft estimate's expiry, tax rate or notes."""
        estimate = await self.get_by_id(estimate_id)
        data = update_data.model_dump(exclude_unset=True)
        customer_notes = data.pop("customer_notes", None)

        if data:
            self._assert_editable(estimate)
        for field, value in data.items():
            setattr(estimate, field, value)

        if customer_notes is not None:
            estimate.customer_notes = customer_notes

        return await self._recalculate_and_save(estimate)

    # --- status machine -----------------------------------------------------

    async def update_status(
        self,
        estimate_id: UUID | str,
        new_status: str,
        reason: str | None = None,
    ) -> Estimate:
        """Transition an estimate's status, enforcing the status machine."""
        try:
            new_status = EstimateStatus(new_status.upper()).value
        except (ValueError, AttributeError):
            allowed = ", ".join(s.value for s in EstimateStatus)
            raise BusinessRuleError(f"Invalid estimate status: {new_status}. Allowed: {allowed}")

        estimate = await self.get_by_id(estimate_id)
        if estimate.status == new_status:
            return estimate

        if not is_transition_allowed(estimate.status, new_status):
            allowed = ESTIMATE_STATUS_TRANSITIONS.get(estimate.status, [])
            raise BusinessRuleError(
                f"Cannot transition estimate status from '{estimate.status}' to "
                f"'{new_status}'. Allowed: {allowed or 'none (terminal state)'}"
            )

        if new_status == EstimateStatus.SENT.value:
            self._assert_sendable(estimate)

        if new_status in (EstimateStatus.EXPIRED.value, EstimateStatus.CANCELLED.value) and reason:
            estimate.decline_reason = reason

        if new_status in _DECIDED_STATUSES:
            estimate.decided_at = _utcnow()

        estimate.status = new_status
        if new_status == EstimateStatus.SENT.value:
            estimate.sent_at = _utcnow()

        await self.db.commit()
        logger.info("Estimate %s -> %s", estimate.estimate_number, new_status)
        return await self.get_by_id(estimate_id)

    @staticmethod
    def _assert_sendable(estimate: Estimate) -> None:
        """An estimate must have billable lines and a live expiry to send."""
        if not estimate.items:
            raise BusinessRuleError("Cannot send an estimate with no line items")
        if estimate.is_expired:
            raise BusinessRuleError(
                "Cannot send an estimate whose valid_until date has already passed"
            )

    async def send(self, estimate_id: UUID | str) -> Estimate:
        """Send a draft estimate to the customer for approval.

        Sending is the moment the customer is asked to do something, so it is the
        moment they are told. The event is queued here and written by the same
        save that commits the status change, so a notification can never outlive
        an estimate that was rolled back.
        """
        estimate = await self.update_status(estimate_id, EstimateStatus.SENT.value)
        # Published *after* the status change committed, so a failed send leaves
        # no queued event behind for some later unrelated save to deliver.
        self.events.publish(
            estimate_ready_event(
                estimate.customer_id, estimate.estimate_number, estimate.id
            )
        )
        await self._flush_events()
        return estimate

    async def cancel(
        self, estimate_id: UUID | str, reason: str | None = None
    ) -> Estimate:
        """Cancel an estimate."""
        return await self.update_status(estimate_id, EstimateStatus.CANCELLED.value, reason)

    # --- customer approval --------------------------------------------------

    async def decide_item(
        self,
        estimate_id: UUID | str,
        item_id: UUID | str,
        decision: ItemDecision,
    ) -> Estimate:
        """Record the customer's decision on one estimate line.

        Decisions are per line, so a customer can approve the safety-critical
        work and decline the optional extras. The estimate status then follows
        from the set of decisions: any decision made but not all -> partially
        approved, all made with at least one approval -> approved, all declined
        -> declined.
        """
        estimate = await self.get_by_id(estimate_id)

        if estimate.is_expired:
            # Persist the expiry so a stale estimate stops looking open.
            if estimate.status == EstimateStatus.SENT.value:
                estimate.status = EstimateStatus.EXPIRED.value
                estimate.decided_at = _utcnow()
                await self.db.commit()
                await self.get_by_id(estimate_id)
            raise BusinessRuleError(
                f"Estimate {estimate.estimate_number} expired on "
                f"{estimate.valid_until.isoformat()} and can no longer be approved"
            )

        if estimate.status not in DECIDABLE_STATUSES:
            raise BusinessRuleError(
                f"Estimate is {estimate.status}; only a sent estimate can be approved "
                "or declined"
            )

        item = next((i for i in estimate.items if str(i.id) == str(item_id)), None)
        if not item:
            raise NotFoundError(f"Estimate item {item_id} not found on estimate {estimate_id}")

        if item.status != EstimateItemStatus.PENDING.value:
            raise ConflictError(
                f"Line '{item.description}' was already {item.status.lower()} and "
                "cannot be decided again"
            )

        item.status = decision.decision
        if decision.notes is not None:
            item.customer_notes = decision.notes

        self._sync_status_from_decisions(estimate)
        await self.db.commit()
        logger.info(
            "Estimate %s line '%s' -> %s", estimate.estimate_number, item.description, decision.decision
        )
        return await self.get_by_id(estimate_id)

    @staticmethod
    def _sync_status_from_decisions(estimate: Estimate) -> None:
        """Derive the estimate status from its lines' decisions."""
        statuses = [i.status for i in estimate.items]
        pending = EstimateItemStatus.PENDING.value
        approved = EstimateItemStatus.APPROVED.value
        declined = EstimateItemStatus.DECLINED.value

        if pending in statuses:
            if any(s in (approved, declined) for s in statuses):
                estimate.status = EstimateStatus.PARTIALLY_APPROVED.value
        elif all(s == declined for s in statuses):
            estimate.status = EstimateStatus.DECLINED.value
        else:
            estimate.status = EstimateStatus.APPROVED.value

        if estimate.status in _DECIDED_STATUSES and estimate.decided_at is None:
            estimate.decided_at = _utcnow()

    # --- deletion -----------------------------------------------------------

    async def delete_estimate(self, estimate_id: UUID | str) -> None:
        """Delete an estimate that the customer has not acted on."""
        estimate = await self.get_by_id(estimate_id)
        if estimate.status in UNDELETABLE_STATUSES:
            raise BusinessRuleError(
                f"Estimate is {estimate.status} and can no longer be deleted; "
                "cancel it instead"
            )
        await self.db.delete(estimate)
        await self.db.commit()
        logger.info("Estimate deleted: %s", estimate.estimate_number)
