"""Customer-facing translations of the shop's internal statuses.

The shop works in precise states — ``QC_PASSED``, ``PARTIALLY_APPROVED``,
``NO_SHOW`` — because they drive what staff are allowed to do next. None of them
mean anything to a customer who wants to know one thing: is my car ready, and do
you need anything from me.

So each status is paired with a plain label and a sentence saying what happens
next or what is being asked of the customer. The mapping is **derived**, never
stored: there is no second status field to fall out of step with the real one, and
a new internal status that is not listed here shows up as the raw value rather
than as a confidently wrong sentence.
"""

from __future__ import annotations

from app.common.schemas import BaseSchema


class CustomerStatus(BaseSchema):
    """An internal status, in words a customer can act on."""

    status: str
    label: str
    detail: str
    # True when the customer is the one being waited on: an estimate to approve,
    # an invoice to pay. Drives the "action needed" badge in the portal.
    needs_customer: bool = False


# Repair orders: what stage the work is at.
REPAIR_ORDER_STATUS: dict[str, CustomerStatus] = {
    "DRAFT": CustomerStatus(
        status="DRAFT",
        label="Booked",
        detail="Your visit is booked in. We will confirm the time shortly.",
    ),
    "APPROVED": CustomerStatus(
        status="APPROVED",
        label="Approved",
        detail="You have approved this work. It is waiting for a bay.",
    ),
    "IN_PROGRESS": CustomerStatus(
        status="IN_PROGRESS",
        label="Work in progress",
        detail="A technician is working on your vehicle.",
    ),
    "ON_HOLD": CustomerStatus(
        status="ON_HOLD",
        label="On hold",
        detail="Work has paused while we sort something out. The shop will be in touch.",
    ),
    "COMPLETED": CustomerStatus(
        status="COMPLETED",
        label="Work finished",
        detail="The work is done and is being checked over.",
    ),
    "QC_PASSED": CustomerStatus(
        status="QC_PASSED",
        label="Ready for pickup",
        detail="Your vehicle has passed its checks and is ready to collect.",
    ),
    "DELIVERED": CustomerStatus(
        status="DELIVERED",
        label="Collected",
        detail="This visit is closed. Thank you for coming to us.",
    ),
    "CANCELLED": CustomerStatus(
        status="CANCELLED",
        label="Cancelled",
        detail="This visit was cancelled and nothing is owed for it.",
    ),
}

# Appointments: where the booking is up to.
APPOINTMENT_STATUS: dict[str, CustomerStatus] = {
    "REQUESTED": CustomerStatus(
        status="REQUESTED",
        label="Requested",
        detail="We have your request and will confirm the time shortly.",
    ),
    "CONFIRMED": CustomerStatus(
        status="CONFIRMED",
        label="Confirmed",
        detail="Your appointment is confirmed. See you then.",
    ),
    "CHECKED_IN": CustomerStatus(
        status="CHECKED_IN",
        label="Checked in",
        detail="Your vehicle is with us and work is about to start.",
    ),
    "IN_SERVICE": CustomerStatus(
        status="IN_SERVICE",
        label="In the workshop",
        detail="Your vehicle is being worked on.",
    ),
    "COMPLETED": CustomerStatus(
        status="COMPLETED",
        label="Finished",
        detail="The work is complete. Your vehicle is being checked before handover.",
    ),
    "CANCELLED": CustomerStatus(
        status="CANCELLED",
        label="Cancelled",
        detail="This appointment was cancelled.",
    ),
    "NO_SHOW": CustomerStatus(
        status="NO_SHOW",
        label="Missed appointment",
        detail="We were waiting for you. Please call to rebook.",
    ),
}

# Estimates: what, if anything, is being asked of the customer.
ESTIMATE_STATUS: dict[str, CustomerStatus] = {
    "DRAFT": CustomerStatus(
        status="DRAFT",
        label="Being prepared",
        detail="The shop is putting this estimate together.",
    ),
    "SENT": CustomerStatus(
        status="SENT",
        label="Awaiting your approval",
        detail="Please review the items and approve or decline each one.",
        needs_customer=True,
    ),
    "PARTIALLY_APPROVED": CustomerStatus(
        status="PARTIALLY_APPROVED",
        label="Partly approved",
        detail="Some items are approved. You can still decide on the rest.",
        needs_customer=True,
    ),
    "APPROVED": CustomerStatus(
        status="APPROVED",
        label="Approved",
        detail="You have approved this work and the shop is scheduling it.",
    ),
    "DECLINED": CustomerStatus(
        status="DECLINED",
        label="Declined",
        detail="You declined this work, so nothing further will be done.",
    ),
    "EXPIRED": CustomerStatus(
        status="EXPIRED",
        label="Expired",
        detail="This estimate is past its date. Ask the shop for a fresh one.",
    ),
    "CANCELLED": CustomerStatus(
        status="CANCELLED",
        label="Cancelled",
        detail="This estimate was withdrawn by the shop.",
    ),
}

# Invoices: what is owed.
INVOICE_STATUS: dict[str, CustomerStatus] = {
    "DRAFT": CustomerStatus(
        status="DRAFT",
        label="Being prepared",
        detail="The shop is preparing your invoice.",
    ),
    "ISSUED": CustomerStatus(
        status="ISSUED",
        label="Payment due",
        detail="Please settle this invoice by the due date.",
        needs_customer=True,
    ),
    "PARTIALLY_PAID": CustomerStatus(
        status="PARTIALLY_PAID",
        label="Part paid",
        detail="Thank you — there is a balance still outstanding.",
        needs_customer=True,
    ),
    "PAID": CustomerStatus(
        status="PAID",
        label="Paid",
        detail="This invoice is settled in full. Nothing further is owed.",
    ),
    "VOID": CustomerStatus(
        status="VOID",
        label="Cancelled",
        detail="This invoice was written off and nothing is owed.",
    ),
}

# Service requests, before they become an appointment. A customer's own words
# for their own request are "sent" and "we're looking at it" — the shop's are
# NEW, IN_REVIEW and the rest.
SERVICE_REQUEST_STATUS: dict[str, CustomerStatus] = {
    "NEW": CustomerStatus(
        status="NEW",
        label="Sent",
        detail="We have your request and will be in touch to arrange a time.",
    ),
    "IN_REVIEW": CustomerStatus(
        status="IN_REVIEW",
        label="Being looked at",
        detail="The shop is reviewing what you have asked for.",
    ),
    "APPROVED": CustomerStatus(
        status="APPROVED",
        label="Accepted",
        detail="We can do this work. It will be booked in shortly.",
    ),
    "REJECTED": CustomerStatus(
        status="REJECTED",
        label="Not possible",
        detail="We are not able to do this work. The shop will explain why.",
    ),
    "CONVERTED": CustomerStatus(
        status="CONVERTED",
        label="Booked in",
        detail="This request has been turned into an appointment.",
    ),
}


def describe(status: str, mapping: dict[str, CustomerStatus]) -> CustomerStatus:
    """Translate an internal status, falling back to the raw value.

    An unmapped status is shown as itself rather than guessed at: a wrong
    sentence about a customer's money is worse than an unfamiliar word, and the
    raw value is at least traceable.
    """
    if status in mapping:
        return mapping[status]
    return CustomerStatus(
        status=status,
        label=status.replace("_", " ").title(),
        detail="The shop will explain this one.",
        needs_customer=False,
    )


__all__ = [
    "APPOINTMENT_STATUS",
    "ESTIMATE_STATUS",
    "INVOICE_STATUS",
    "REPAIR_ORDER_STATUS",
    "SERVICE_REQUEST_STATUS",
    "CustomerStatus",
    "describe",
]
