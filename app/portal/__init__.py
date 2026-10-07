"""Customer portal domain.

Everything a customer is allowed to see about their own account, in one place
and behind one check.

The portal is a **view**, not a second set of records. It reads the same
estimates, repair orders, invoices and payments the shop works with and presents
them in the customer's own words; it never copies them and never invents a
simplified status of its own that could disagree with the shop's. A customer sees
their own data because of a single rule enforced in one place: every query is
filtered by the customer the signed-in user belongs to, and any record named
directly has to belong to that customer or it is reported as not found.

The statuses are the one thing that *is* translated. ``QC_PASSED`` means nothing
to somebody who just wants to know whether their car is ready, so each internal
status is paired with a plain label and a sentence saying what happens next.
"""
