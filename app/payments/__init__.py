"""Payments domain.

Money the shop has actually taken. A payment is an event, not a document: it is
recorded once and never edited, because a record of what a customer paid is a
fact about the till, not a draft somebody tidies up. The one thing that can be
done to a payment is void it, which leaves the row in place with a reason on it
and puts the money back on the invoice's balance.

A payment is always against an :class:`~app.invoices.models.Invoice`, and it is
the only thing that can move an invoice into ``PARTIALLY_PAID`` or ``PAID``.
Partial payments are ordinary rather than an exception: an invoice may be settled
across several visits, and the balance is derived from what has been received.
"""
