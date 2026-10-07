"""Reports and analytics domain.

A report here is a **reading, not a record**. Every number is computed from the
invoices, repair orders, labor records, parts and customers the shop already
keeps, at the moment it is asked for. There is no summary table, no nightly job
and no cached dashboard row, and the reason is the one the whole project keeps
coming back to: a stored number is a second source of truth, and it starts
disagreeing the moment an invoice is voided, a payment is reversed, or a repair
order is reopened for rework. A dashboard that is stale is worse than no
dashboard, because it is believed.

So this module has **no models and no migration**. Every report is a projection:
if the ledger says the shop took 4,200 this month, the report says 4,200, and it
says it because it just asked the payments table.

The two things that are deliberately *not* reports:

* **Money is counted twice, on purpose.** ``billed`` is what the shop charged
  (invoices that left the counter) and ``collected`` is what the till actually
  took (payments recorded, net of voids). Collapsing them into one "revenue"
  number is how a shop ends a month thinking it earned money it was never paid.
  Both are reported side by side and neither is labelled the other.
* **A period is required.** An open-ended revenue query over an append-only
  ledger gets slower every year and nobody ever wanted the answer. The window
  defaults to the last 30 days, and ``start_date`` after ``end_date`` is refused
  rather than quietly returning nothing.
"""
